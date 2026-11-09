"""Reportes sintéticos para entrenar y evaluar el clasificador (docs/diseno.md §11 y §15).

    python -m guadua.classifier.synthetic          # reescribe data/sinteticos/reportes.csv

Se generan una vez, con semilla fija, y el CSV se versiona: así todos los nodos entrenan
con lo mismo y el modelo sale idéntico. Cada fila junta una situación ("se cayó la casa")
con una condición de las personas ("no respira"), que es la que fija el nivel. Se agregan
huecos (lugar, persona, número) y ruido: jerga, faltas de ortografía, texto cortado,
mayúsculas y segunda mano ("me dijeron que...").

**El conjunto de prueba se separa por combinación**, no por fila: uno de cada cuatro pares
(situación, condición) va solo a `test`, y la última condición de cada nivel nunca se
entrena (frases que el clasificador nunca vio). La columna `template` dice de qué par
salió cada fila ("situación | condición"). Las filas `ambiguo` (señales mezcladas o
vagas) quedan aparte para evaluar al agente de triaje (N8); tampoco se entrenan.

El tipo de incidente es el que marcaría el ciudadano en la PWA; a veces marca "otro" u
otra categoría que no coincide con la gravedad. Hay contraejemplos a propósito: animales
heridos, heridos leves ya atendidos, daños sin personas.
Las banderas de la cabecera no se incluyen: la evaluación supone que no marcó ninguna, que
es el caso difícil para las reglas.

Limitación declarada: es texto sintético, no reportes reales.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Incident

DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "sinteticos" / "reportes.csv"
SEED = 20261109
VARIANTS_PER_PAIR = 5  # por cada (situación, condición)
VARIANTS_PER_AMBIGUOUS = 40
TEST_EVERY = 4  # el par (i, j) va a test si (i + j) % 4 == 3
FIELDS = ("text", "incident", "label", "split", "template")

A, C, INC, G, H, V, OTRO = (  # abreviaturas para las tablas de abajo
    Incident.ATRAPADOS,
    Incident.COLAPSO,
    Incident.INCENDIO,
    Incident.FUGA_GAS,
    Incident.HERIDO,
    Incident.VIA_BLOQUEADA,
    Incident.OTRO,
)

# Cada reporte = situación ("qué pasó") + condición ("cómo están las personas"). El nivel
# lo fija la condición. Huecos: {lugar}, {quien}, {n}, {animal}. Si la condición trae un tipo de
# incidente, ese es el que marcaría el ciudadano; si no, el de la situación.
type Phrase = tuple[Incident | None, str]

SITUACIONES_DANO: tuple[Phrase, ...] = (
    (C, "se cayo la casa en {lugar}"),
    (C, "se derrumbo el edificio en {lugar}"),
    (C, "se vino abajo el muro en {lugar}"),
    (OTRO, "hubo un deslizamiento en {lugar}"),
    (C, "con el temblor se cayo el techo en {lugar}"),
    (C, "se desplomo la iglesia de {lugar}"),
    (H, "choco un bus en {lugar}"),
    (OTRO, "la avalancha de tierra llego a {lugar}"),
)
SITUACIONES_INFO: tuple[Phrase, ...] = (
    (OTRO, "se fue la luz en {lugar}"),
    (OTRO, "ya paso el temblor en {lugar}"),
    (OTRO, "hola escribo desde {lugar}"),
    (OTRO, "despues del temblor en {lugar}"),
    (OTRO, "estamos en el albergue de {lugar}"),
    (OTRO, "en {lugar}"),
    (OTRO, "les escribo de {lugar}"),
    (OTRO, "aqui en {lugar}"),
)
CONDICIONES: dict[Urgency, tuple[Phrase, ...]] = {
    Urgency.CRITICA: (
        (A, "hay {n} personas atrapadas"),
        (H, "{quien} no respira"),
        (H, "{quien} esta inconsciente"),
        (H, "{quien} esta sangrando mucho"),
        (None, "{quien} quedo debajo y no se mueve"),
        (None, "se oyen gritos debajo de lo que quedo"),
        (H, "{quien} esta morado y no reacciona"),
        (None, "la familia quedo adentro"),
        (H, "{quien} perdio el conocimiento"),
        (None, "hay un nino tapado por la tierra"),
        (H, "{quien} tiene la cabeza abierta y no despierta"),
        (OTRO, "{quien} se esta ahogando"),
        (V, "un carro arrollo a {quien} y no se mueve"),
        (G, "{quien} se desmayo por el gas"),
        (None, "no encontramos a {quien}, estaba adentro"),
    ),
    Urgency.ALTA: (
        (H, "{quien} tiene una pierna fracturada"),
        (H, "{quien} se golpeo la cabeza pero habla"),
        (H, "hay {n} heridos que caminan"),
        (None, "no sabemos si habia alguien"),
        (H, "{quien} tiene quemaduras en el brazo"),
        (H, "{quien} tiene dolor en el pecho pero respira"),
        (H, "{quien} no puede caminar"),
        (H, "hay gente herida con cortadas"),
        (G, "huele fuerte a gas y hay gente cerca"),
        (H, "{quien} esta mareado y vomita"),
        (None, "hay cables con chispas cerca de la gente"),
        (V, "un carro atropello a {quien}, esta consciente"),
        (OTRO, "{quien} tuvo convulsiones pero ya respira"),
        (G, "{quien} esta mareado por el olor a gas"),
        (None, "la estructura sigue crujiendo con gente al lado"),
    ),
    Urgency.MEDIA: (
        (None, "no hay heridos"),
        (None, "todos salimos bien pero la casa quedo mal"),
        (V, "quedo tapada la via"),
        (H, "{quien} tiene raspones leves"),
        (None, "estamos afuera, nadie adentro"),
        (None, "la casa estaba vacia"),
        (None, "hay grietas pero nadie herido"),
        (V, "no pasan los carros"),
        (H, "{quien} se torcio el tobillo"),
        (None, "{quien} esta nervioso pero bien"),
        (OTRO, "se rompio un tubo de agua"),
        (H, "hay un {animal} herido, las personas estan bien"),
        (H, "{quien} tiene una cortada pequena, ya la curamos"),
        (C, "se cayo una pared del patio, no habia nadie"),
        (V, "hay piedras en toda la calle"),
    ),
    Urgency.BAJA: (
        (None, "todos estamos bien"),
        (None, "se rompio un vidrio"),
        (None, "necesitamos agua y comida"),
        (None, "cuando vuelve la senal"),
        (None, "se cayeron unas tejas, nada grave"),
        (None, "quiero saber si {quien} esta en un albergue"),
        (None, "se cayeron cosas de los estantes"),
        (None, "donde hay un punto de encuentro"),
        (None, "estamos a salvo"),
        (None, "el perro se escapo con el susto"),
        (None, "ya llegaron los bomberos, gracias"),
        (H, "hay un {animal} herido en la calle"),
        (H, "el {animal} de la vecina esta cojo"),
        (C, "se cayo el techo del gallinero, no hay personas"),
        (None, "el {animal} se subio al techo y no puede bajar"),
        (None, "no funciona el internet"),
    ),
}
CONECTORES = (", ", " y ", ". ", " ", " ahora ")

AMBIGUOUS: tuple[tuple[Urgency, Incident, str], ...] = (
    (Urgency.ALTA, C, "se oyo un estruendo en {lugar}, no se si hay gente"),
    (Urgency.CRITICA, OTRO, "dicen que hay gente adentro de la casa caida en {lugar} pero no se"),
    (Urgency.ALTA, H, "{quien} esta muy palido y casi no habla en {lugar}"),
    (Urgency.MEDIA, G, "creo que huele a gas en {lugar}, o puede ser la basura"),
    (Urgency.CRITICA, C, "no encontramos a {quien} desde que se cayo el edificio en {lugar}"),
    (Urgency.ALTA, OTRO, "hay humo en {lugar}, no se de donde sale"),
    (Urgency.MEDIA, C, "la casa de al lado en {lugar} suena raro, como crujiendo"),
    (Urgency.ALTA, H, "{quien} se queja mucho del estomago despues del golpe en {lugar}"),
)

LUGARES = (
    "El Cable", "Av. Santander", "Chipre", "Centro", "Palermo", "Milán", "La Enea",
    "Villamaría", "San José", "Fátima", "La Sultana", "Bosques del Norte", "Aranjuez",
    "la calle 23", "la carrera 15", "frente a la iglesia",
)  # fmt: skip
QUIENES = (
    "mi mama", "mi papa", "el vecino", "la senora de la tienda", "un nino", "mi abuelo",
    "mi abuela", "un señor", "una muchacha", "mi hermano", "el celador", "una profesora",
)  # fmt: skip
ANIMALES = ("gato", "perro", "gallina", "caballo", "loro", "vaca", "conejo", "gatico", "perrito")
NUMEROS = ("2", "3", "4", "dos", "tres", "varias", "como 5", "unas")
PREFIJOS = ("", "", "", "", "ayuda ", "auxilio ", "uy ", "ome ", "parce ", "porfa ", "urgente ")
SUFIJOS = ("", "", "", "", " porfa", " rapido", " ayuda", " por favor vengan", " ya", " !!")
SEGUNDA_MANO = ("me dijeron que ", "dicen que ", "vi en whatsapp que ", "un vecino dice que ")
# Cambios típicos al escribir rápido en el celular.
SUSTITUCIONES = (
    ("s", "z"), ("c", "s"), ("ll", "y"), ("b", "v"), ("v", "b"), ("h", ""), ("qu", "k"),
    ("ado", "ao"), ("ida", "ia"), ("y", "i"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Row:
    text: str
    incident: Incident
    label: Urgency
    split: str  # "train", "test" o "ambiguo"
    template: str  # "situación | condición", o la plantilla si es ambigua


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    match rng.randrange(4):
        case 0:  # se come una letra
            return word[:i] + word[i + 1 :]
        case 1:  # cambia dos de lugar
            return word[: i - 1] + word[i] + word[i - 1] + word[i + 1 :]
        case 2:  # repite una
            return word[:i] + word[i] + word[i:]
        case _:
            old, new = rng.choice(SUSTITUCIONES)
            return word.replace(old, new, 1)


def _noisy(text: str, rng: random.Random) -> str:
    words = [_typo(w, rng) if rng.random() < 0.12 else w for w in text.split()]
    if len(words) > 6 and rng.random() < 0.1:  # se corta el mensaje
        words = words[: -rng.randint(1, 2)]
    opening = rng.choice(SEGUNDA_MANO) if rng.random() < 0.15 else rng.choice(PREFIJOS)
    text = opening + " ".join(words) + rng.choice(SUFIJOS)
    if rng.random() < 0.2:
        text = text.upper()
    return text


def _fill(template: str, rng: random.Random) -> str:
    return template.format(
        lugar=rng.choice(LUGARES),
        quien=rng.choice(QUIENES),
        n=rng.choice(NUMEROS),
        animal=rng.choice(ANIMALES),
    )


# La categoría que marca el ciudadano no siempre coincide con la gravedad ("Herido" para un
# gato herido): así el clasificador aprende a mirar el texto y no solo la categoría.
# Atrapados e incendio no se eligen al azar: en la PWA prenden banderas que fijan crítica.
MISMATCH = (Incident.COLAPSO, Incident.FUGA_GAS, Incident.HERIDO, Incident.VIA_BLOQUEADA)


def _chosen_incident(incident: Incident, rng: random.Random) -> Incident:
    draw = rng.random()
    if draw < 0.15:
        return Incident.OTRO  # marcó "otro"
    if draw < 0.30:
        return rng.choice(MISMATCH)  # marcó otra categoría
    return incident


def _variants(
    template: str, incident: Incident, label: Urgency, split: str, n: int, rng: random.Random
) -> Iterator[Row]:
    seen: set[str] = set()
    for _ in range(n):
        text = _noisy(_fill(template, rng), rng)
        if text in seen:
            continue
        seen.add(text)
        chosen = _chosen_incident(incident, rng)
        yield Row(text, chosen, label, split, template)


def _join(situation: str, condition: str, rng: random.Random) -> str:
    if rng.random() < 0.25:  # a veces empieza por las personas
        return condition + rng.choice(CONECTORES) + situation
    return situation + rng.choice(CONECTORES) + condition


def generate(seed: int = SEED) -> list[Row]:
    rng = random.Random(seed)
    rows: list[Row] = []
    for label, conditions in CONDICIONES.items():
        situations = SITUACIONES_INFO if label == Urgency.BAJA else SITUACIONES_DANO
        held_out = len(conditions) - 1  # la última condición de cada nivel: solo en test
        for i, (sit_incident, situation) in enumerate(situations):
            for j, (cond_incident, condition) in enumerate(conditions):
                unseen = j == held_out or (i + j) % TEST_EVERY == TEST_EVERY - 1
                incident = cond_incident or sit_incident or Incident.OTRO
                split = "test" if unseen else "train"
                pair = f"{situation} | {condition}"
                for _ in range(VARIANTS_PER_PAIR):
                    template = _join(situation, condition, rng)
                    rows += (
                        Row(r.text, r.incident, r.label, r.split, pair)
                        for r in _variants(template, incident, label, split, 1, rng)
                    )
    for label, incident, template in AMBIGUOUS:
        rows += _variants(template, incident, label, "ambiguo", VARIANTS_PER_AMBIGUOUS, rng)
    # Un mismo texto nunca queda en dos conjuntos: gana el primero.
    first: dict[str, Row] = {}
    for row in rows:
        first.setdefault(row.text, row)
    return list(first.values())


def write(rows: list[Row], path: Path = DATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(FIELDS)
        for r in rows:
            writer.writerow(
                (r.text, r.incident.name.lower(), r.label.name.lower(), r.split, r.template)
            )


def load(path: Path = DATA_PATH) -> list[Row]:
    with path.open(encoding="utf-8", newline="") as f:
        return [
            Row(
                r["text"],
                Incident[r["incident"].upper()],
                Urgency[r["label"].upper()],
                r["split"],
                r["template"],
            )
            for r in csv.DictReader(f)
        ]


if __name__ == "__main__":
    rows = generate()
    write(rows)
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        counts[r.split, r.label.name.lower()] = counts.get((r.split, r.label.name.lower()), 0) + 1
    print(f"{len(rows)} reportes → {DATA_PATH}")
    for (split, label), n in sorted(counts.items()):
        print(f"  {split:8} {label:8} {n}")
