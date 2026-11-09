"""Pruebas de la cola de comunicación con envejecimiento (Agente 4, N11).

Terminado cuando:
    Críticos salen primero; espera máxima de los bajos medida y acotada.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from guadua.channel.profiles import LORA
from guadua.channel.simulated import SimulatedNetwork
from guadua.classifier.urgency import Urgency
from guadua.protocol.fragments import (
    Ack,
    Reassembler,
    decode_fragment,
    encode_ack,
)
from guadua.protocol.header import Header as BinaryHeader
from guadua.protocol.header import Incident, MessageType, People, encode
from guadua.services.queue import (
    CONTROL_WEIGHT,
    CommunicationQueue,
    ItemStatus,
)


class MockClock:
    """Reloj simulado para pruebas deterministas y sin esperas reales."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class DummyTransport:
    """Transporte en memoria para pruebas directas."""

    def __init__(self, mtu: int = 140) -> None:
        self.mtu = mtu
        self.sent: list[tuple[str, bytes]] = []

    async def send(self, destination: str, frame: bytes) -> None:
        self.sent.append((destination, frame))

    async def receive(self, timeout: float) -> tuple[str, bytes] | None:
        return None


def _dummy_header(msg_type: MessageType = MessageType.REPORTE) -> bytes:
    hdr = BinaryHeader(
        msg_type=msg_type,
        device_id=42,
        seq=1,
        device_time=100,
        incident=Incident.COLAPSO,
        people=People.UNA,
        flags=0,
        geohash=None,
        barrio=1,
    )
    return encode(hdr)


# --- 1. Críticos salen primero -------------------------------------------------------------


def test_critical_first_when_enqueued_together() -> None:
    """Con arribo simultáneo, los mensajes críticos salen antes que los de menor urgencia."""
    clock = MockClock()
    q = CommunicationQueue(clock=clock)

    q.enqueue(b"baja", destination="B", urgency=Urgency.BAJA)
    q.enqueue(b"critica", destination="B", urgency=Urgency.CRITICA)
    q.enqueue(b"alta", destination="B", urgency=Urgency.ALTA)
    q.enqueue(b"media", destination="B", urgency=Urgency.MEDIA)

    order: list[Urgency] = []
    for _ in range(4):
        item = q.select_next()
        assert item is not None
        order.append(item.urgency)
        # Marcar como entregado para simular despacho
        item.status = ItemStatus.ENTREGADO

    assert order == [
        Urgency.CRITICA,
        Urgency.ALTA,
        Urgency.MEDIA,
        Urgency.BAJA,
    ]


# --- 2. Desempate por tamaño ---------------------------------------------------------------


def test_tie_break_by_smaller_size() -> None:
    """A igual prioridad y antigüedad, sale primero el mensaje más pequeño en bytes."""
    clock = MockClock()
    q = CommunicationQueue(clock=clock)

    q.enqueue(b"x" * 100, destination="B", urgency=Urgency.ALTA)
    q.enqueue(b"x" * 20, destination="B", urgency=Urgency.ALTA)
    q.enqueue(b"x" * 60, destination="B", urgency=Urgency.ALTA)

    order_sizes = []
    for _ in range(3):
        item = q.select_next()
        assert item is not None
        order_sizes.append(len(item.payload))
        item.status = ItemStatus.ENTREGADO

    assert order_sizes == [20, 60, 100]


# --- 3. Prioridad de fotos y control -------------------------------------------------------


def test_photo_fragments_do_not_delay_text() -> None:
    """Los fragmentos de foto tienen peso menor que cualquier texto y nunca lo retrasan."""
    clock = MockClock()
    q = CommunicationQueue(clock=clock)

    # Fragmento de foto (peso base 5.0) y texto de urgencia baja (peso base 10.0)
    q.enqueue(
        b"foto_data",
        destination="B",
        msg_type=MessageType.FRAGMENTO_FOTO,
    )
    q.enqueue(
        b"texto_baja",
        destination="B",
        urgency=Urgency.BAJA,
    )

    first = q.select_next()
    assert first is not None
    assert first.msg_type == MessageType.REPORTE
    assert first.payload == b"texto_baja"


def test_control_messages_have_top_priority() -> None:
    """Los mensajes de control ('Estoy a salvo', ACK, SOS) tienen prioridad inmediata."""
    clock = MockClock()
    q = CommunicationQueue(clock=clock)

    q.enqueue(b"critico_normal", destination="B", urgency=Urgency.CRITICA)
    q.enqueue(
        b"estoy_a_salvo",
        destination="B",
        msg_type=MessageType.A_SALVO,
    )
    q.enqueue(
        b"sos_escombros",
        destination="B",
        msg_type=MessageType.SOS_ESCOMBROS,
    )

    item1 = q.select_next()
    assert item1 is not None
    assert item1.base_weight == CONTROL_WEIGHT
    item1.status = ItemStatus.ENTREGADO

    item2 = q.select_next()
    assert item2 is not None
    assert item2.base_weight == CONTROL_WEIGHT
    item2.status = ItemStatus.ENTREGADO

    item3 = q.select_next()
    assert item3 is not None
    assert item3.payload == b"critico_normal"


# --- 4. Envejecimiento y prevención de inanición ------------------------------------------


def test_aging_promotes_low_priority_over_newer_high() -> None:
    """Un reporte bajo en cola va ganando prioridad hasta superar a mensajes más urgentes nuevos."""
    clock = MockClock()
    # factor = 3.0 puntos/minuto
    q = CommunicationQueue(clock=clock, aging_factor=3.0)

    # t = 0 min: Encolar reporte bajo (prioridad inicial = 10.0)
    low = q.enqueue(b"bajo", destination="B", urgency=Urgency.BAJA)

    # t = 0 min: Si llega uno de urgencia Alta (70.0), el nuevo gana
    assert q.effective_priority(low) == 10.0

    # Avanzar 25 minutos: low tiene 10 + 3 * 25 = 85.0
    clock.advance(25 * 60)
    high_fresh = q.enqueue(b"alta_nueva", destination="B", urgency=Urgency.ALTA)
    assert q.effective_priority(low) == 85.0
    assert q.effective_priority(high_fresh) == 70.0
    # Ahora el bajo supera al alta recién llegado
    assert q.select_next() == low

    # Avanzar otros 10 minutos (total 35 minutos): low tiene 10 + 3 * 35 = 115.0
    clock.advance(10 * 60)
    crit_fresh = q.enqueue(b"critica_nueva", destination="B", urgency=Urgency.CRITICA)
    assert q.effective_priority(low) == 115.0
    assert q.effective_priority(crit_fresh) == 100.0
    # Supera incluso a un crítico recién llegado
    assert q.select_next() == low


# --- 5. Criterio de Plan.md: Espera máxima medida y acotada -------------------------------


def test_measured_and_bounded_max_wait() -> None:
    """Criterio de N11: Críticos salen primero; espera máxima de los bajos medida y acotada.

    Bajo una ráfaga ininterrumpida de mensajes críticos recién llegados, un mensaje
    de baja urgencia nunca espera más tiempo que la cota teórica calculada:
        T_max = (W_critica - W_baja) / aging_factor = (100 - 10) / 3.0 = 30 minutos.
    """
    clock = MockClock(start=0.0)
    factor = 3.0  # puntos por minuto
    q = CommunicationQueue(clock=clock, aging_factor=factor)

    # Cota teórica
    theoretical_bound_minutes = q.max_wait_minutes(Urgency.BAJA, Urgency.CRITICA)
    assert theoretical_bound_minutes == pytest.approx(30.0)

    # Encolar un mensaje de baja prioridad en t=0
    low_item = q.enqueue(b"reporte_bajo", destination="B", urgency=Urgency.BAJA)

    # Simular ráfaga de críticos: cada 30 segundos llega un nuevo reporte crítico
    # y se realiza una ventana de transmisión
    step_seconds = 30.0
    low_dispatched = False
    dispatched_items: list[Any] = []
    # Ejecutar hasta que salga el reporte bajo o se exceda la cota con margen
    max_steps = 100  # 100 * 30 s = 50 minutos (bien por encima de los 30 min)
    for burst_count in range(1, max_steps + 1):
        # Entra un crítico nuevo
        q.enqueue(
            f"critico_{burst_count}".encode(),
            destination="B",
            urgency=Urgency.CRITICA,
        )

        # Seleccionar el siguiente según política de Agente 4
        chosen = q.select_next()
        assert chosen is not None

        if chosen.id == low_item.id:
            low_dispatched = True
            dispatched_items.append(chosen)
            break
        else:
            dispatched_items.append(chosen)
            chosen.status = ItemStatus.ENTREGADO

        clock.advance(step_seconds)

    # Medición
    wait_time_seconds = clock.time
    wait_time_minutes = wait_time_seconds / 60.0

    # 1. El reporte bajo fue despachado
    assert low_dispatched is True, "El reporte bajo sufrió inanición (starvation)"

    # 2. Los críticos salieron primero durante los primeros ~30 minutos
    assert len(dispatched_items) > 1
    assert dispatched_items[0].urgency == Urgency.CRITICA

    # 3. La espera máxima medida está acotada por la cota teórica
    # En el momento en que se superan los 30 min, prioridad_efectiva(low) > 100,
    # por lo que debe salir en el primer paso donde t >= 30 min.
    assert wait_time_minutes <= theoretical_bound_minutes + (step_seconds / 60.0), (
        f"Espera medida ({wait_time_minutes:.2f} min) superó la cota "
        f"({theoretical_bound_minutes:.2f} min)"
    )
    assert wait_time_minutes >= theoretical_bound_minutes - (step_seconds / 60.0)


# --- 6. Vencimiento sin borrado (docs/diseno.md §8) ----------------------------------------


def test_expiration_without_deletion() -> None:
    """Reportes sin confirmación por horas pasan a 'por_confirmar'; no se borran."""
    clock = MockClock()
    q = CommunicationQueue(clock=clock, ttl_seconds=3600.0)  # 1 hora

    item = q.enqueue(b"via_bloqueada", destination="B", urgency=Urgency.MEDIA)
    assert item.status == ItemStatus.ENCOLADO

    # A los 30 minutos sigue activo
    clock.advance(1800)
    assert q.select_next() == item

    # A los 65 minutos superó el TTL
    clock.advance(2100)
    expired = q.check_timeouts()
    assert item in expired
    assert item.status == ItemStatus.POR_CONFIRMAR

    # No se borra de la cola: sigue accesible en el inventario y auditoría
    assert item in q.items()
    assert item in q.get_unconfirmed()
    assert len(q) == 0  # no hay activos pendientes de transmisión
    assert q.select_next() is None  # no compite en la ventana de transmisión


# --- 7. Transmisión, fragmentación y ACKs con Transport (N2 + N3) -------------------------


def test_transmit_next_unfragmented() -> None:
    async def scenario() -> None:
        clock = MockClock()
        q = CommunicationQueue(clock=clock)
        transport = DummyTransport(mtu=140)

        item = q.enqueue(b"hola_mundo", destination="B", urgency=Urgency.CRITICA)
        sent_item = await q.transmit_next(transport)
        assert sent_item == item
        assert len(transport.sent) == 1
        assert transport.sent[0] == ("B", b"hola_mundo")
        assert item.status == ItemStatus.TRANSMITIENDO

        # Recibe ACK total 1
        ack = Ack(item.msg_type, item.device_id, item.seq, 1, frozenset({0}))
        assert q.on_ack(ack) is True
        assert item.status == ItemStatus.ENTREGADO
        assert len(q) == 0

    asyncio.run(scenario())


def test_transmit_next_fragmented_and_retransmits_only_missing() -> None:
    async def scenario() -> None:
        clock = MockClock()
        q = CommunicationQueue(clock=clock)
        # MTU de 140 -> fragmentos de 140 - 11 = 129 B
        transport = DummyTransport(mtu=140)

        # 300 B necesita 3 fragmentos (129, 129, 42)
        payload = b"A" * 300
        item = q.enqueue(
            payload,
            destination="B",
            urgency=Urgency.ALTA,
            device_id=99,
            seq=10,
        )

        # Primer intento: envía los 3 fragmentos
        await q.transmit_next(transport)
        assert len(transport.sent) == 3
        assert item.sender is not None
        assert len(item.sender.pending()) == 3

        # El receptor confirma fragmentos 0 y 2 (falta el 1)
        ack_partial = Ack(
            item.msg_type,
            item.device_id,
            item.seq,
            total=3,
            received=frozenset({0, 2}),
        )
        assert q.on_ack(ack_partial) is True
        assert item.status == ItemStatus.TRANSMITIENDO
        assert len(item.sender.pending()) == 1
        assert item.sender.pending()[0].index == 1

        # Segundo intento, pasada la espera de reintento: retransmite SOLO el fragmento 1
        transport.sent.clear()
        clock.advance(q.retry_wait(item))
        await q.transmit_next(transport)
        assert len(transport.sent) == 1
        frag1 = decode_fragment(transport.sent[0][1])
        assert frag1.index == 1

        # Llega ACK completo
        ack_full = Ack(
            item.msg_type,
            item.device_id,
            item.seq,
            total=3,
            received=frozenset({0, 1, 2}),
        )
        q.on_ack(ack_full)
        assert item.status == ItemStatus.ENTREGADO
        assert q.select_next() is None

    asyncio.run(scenario())


def test_process_incoming_frame_decodes_ack() -> None:
    clock = MockClock()
    q = CommunicationQueue(clock=clock)
    item = q.enqueue(b"x", destination="B", urgency=Urgency.ALTA, device_id=7, seq=1)
    ack = Ack(item.msg_type, 7, 1, 1, frozenset({0}))
    raw_ack = encode_ack(ack)

    assert q.process_incoming_frame(raw_ack) is True
    assert item.status == ItemStatus.ENTREGADO
    assert q.process_incoming_frame(b"invalido") is False


# --- 8. Integración con SimulatedNetwork con pérdidas (N3) --------------------------------


def test_queue_over_simulated_network_with_loss() -> None:
    """Prueba de integración end-to-end con canal LoRa simulado con 30 % de pérdida."""

    async def scenario() -> None:
        network = SimulatedNetwork(seed=42, time_scale=0)
        network.link("A", "B", LORA)  # 30 % pérdida
        a_transport = network.endpoint("A")
        b_transport = network.endpoint("B")

        clock = MockClock()
        q = CommunicationQueue(node_id=1, clock=clock)

        # Encolar 2 reportes grandes que requieren fragmentación: uno crítico y uno bajo
        crit = q.enqueue(
            b"CRITICO_DATA_" + b"1" * 350,
            destination="B",
            urgency=Urgency.CRITICA,
            device_id=1,
            seq=1,
        )
        low = q.enqueue(
            b"BAJO_DATA_" + b"2" * 350,
            destination="B",
            urgency=Urgency.BAJA,
            device_id=1,
            seq=2,
        )

        reassembler = Reassembler()
        delivered_at_b: list[bytes] = []

        # Bucle de retransmisión gestionado por la cola: una ventana por segundo, así corre
        # la espera de reintento (N11b)
        for _ in range(400):
            if crit.status == ItemStatus.ENTREGADO and low.status == ItemStatus.ENTREGADO:
                break

            # Transmitir el elemento prioritario
            await q.transmit_next(a_transport)
            clock.advance(1.0)

            # B recibe y responde ACK
            while (packet := await b_transport.receive(timeout=0)) is not None:
                src, frame = packet
                if len(frame) > 11:
                    fragment = decode_fragment(frame)
                    full = reassembler.add(fragment)
                    if full is not None:
                        delivered_at_b.append(full)
                    ack_frame = encode_ack(reassembler.ack_for(fragment))
                    await b_transport.send(src, ack_frame)

            # A recibe ACKs y actualiza la cola
            while (ack_packet := await a_transport.receive(timeout=0)) is not None:
                _, ack_frame = ack_packet
                q.process_incoming_frame(ack_frame)

        assert crit.status == ItemStatus.ENTREGADO
        assert low.status == ItemStatus.ENTREGADO
        assert len(delivered_at_b) == 2
        # El primero entregado debe ser el crítico
        assert delivered_at_b[0] == crit.payload
        assert delivered_at_b[1] == low.payload

    asyncio.run(scenario())


# --- 9. Estadísticas e inspección ---------------------------------------------------------


def test_stats_and_len() -> None:
    clock = MockClock()
    q = CommunicationQueue(clock=clock, aging_factor=3.0)

    assert len(q) == 0
    q.enqueue(b"1", destination="B", urgency=Urgency.BAJA)
    q.enqueue(b"2", destination="B", urgency=Urgency.ALTA)
    assert len(q) == 2

    stats = q.stats()
    assert stats["total"] == 2
    assert stats["active_count"] == 2
    assert stats["by_status"]["encolado"] == 2
    assert stats["aging_factor"] == 3.0


def test_auto_detect_header_fields() -> None:
    clock = MockClock()
    q = CommunicationQueue(clock=clock)
    payload = _dummy_header(MessageType.SOS_ESCOMBROS) + b"nota_extra"
    item = q.enqueue(payload, destination="B")

    assert item.msg_type == MessageType.SOS_ESCOMBROS
    assert item.device_id == 42
    assert item.seq == 1
    assert item.base_weight == CONTROL_WEIGHT


# --- 10. Reintento con espera creciente (N11b) --------------------------------------------


def test_no_reenvia_antes_de_la_espera_y_si_despues() -> None:
    async def scenario() -> None:
        clock = MockClock()
        q = CommunicationQueue(clock=clock, retry_base_s=2.0)
        transport = DummyTransport()
        item = q.enqueue(b"reporte", destination="B", urgency=Urgency.CRITICA, device_id=1, seq=1)

        assert await q.transmit_next(transport) is item
        assert q.waiting_ack(item) and await q.transmit_next(transport) is None
        clock.advance(1.999)
        assert await q.transmit_next(transport) is None  # el ACK puede venir en camino
        clock.advance(0.001)
        assert await q.transmit_next(transport) is item
        assert len(transport.sent) == 2 and item.attempts == 2

    asyncio.run(scenario())


def test_la_espera_se_duplica_hasta_el_tope() -> None:
    q = CommunicationQueue(clock=MockClock(), retry_base_s=2.0, retry_max_s=10.0)
    item = q.enqueue(b"x", destination="B", device_id=1, seq=1)
    waits = []
    for attempts in range(7):
        item.attempts = attempts
        waits.append(q.retry_wait(item))
    assert waits == [0.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
    item.attempts = 10_000
    assert q.retry_wait(item) == 10.0  # sin desbordes con muchos intentos


def test_un_critico_que_espera_su_ack_deja_salir_a_un_bajo() -> None:
    async def scenario() -> None:
        clock = MockClock()
        q = CommunicationQueue(clock=clock, retry_base_s=2.0)
        transport = DummyTransport()
        crit = q.enqueue(b"critico", destination="B", urgency=Urgency.CRITICA, device_id=1, seq=1)
        low = q.enqueue(b"bajo", destination="B", urgency=Urgency.BAJA, device_id=1, seq=2)

        assert await q.transmit_next(transport) is crit
        assert await q.transmit_next(transport) is low  # la ventana no se desperdicia
        assert await q.transmit_next(transport) is None  # los dos esperan su ACK
        clock.advance(2.0)
        assert await q.transmit_next(transport) is crit  # pasada la espera, el crítico primero

    asyncio.run(scenario())


def test_el_ack_corta_los_reintentos() -> None:
    async def scenario() -> None:
        clock = MockClock()
        q = CommunicationQueue(clock=clock)
        transport = DummyTransport()
        item = q.enqueue(b"reporte", destination="B", urgency=Urgency.ALTA, device_id=7, seq=3)
        await q.transmit_next(transport)
        assert q.on_ack(Ack(item.msg_type, 7, 3, 1, frozenset({0})))
        clock.advance(3600.0)
        assert await q.transmit_next(transport) is None
        assert len(transport.sent) == 1 and item.status == ItemStatus.ENTREGADO

    asyncio.run(scenario())


@pytest.mark.parametrize(("base", "maximum"), [(0.0, 10.0), (-1.0, 10.0), (5.0, 4.0)])
def test_parametros_de_reintento_invalidos(base: float, maximum: float) -> None:
    with pytest.raises(ValueError):
        CommunicationQueue(retry_base_s=base, retry_max_s=maximum)
