"""
Taxonomía cerrada de categorías (spec sección 7, pendiente de definir).
Esta lista se inyecta en el prompt del LLM y en la gramática GBNF.
Edítala aquí; ambos sitios se generan/derivan de esta única fuente de verdad
(la gramática GBNF vive en grammar/convocatorias.gbnf y debe mantenerse
sincronizada manualmente si cambias esta lista — ver README).
"""

CATEGORIAS_CANONICAS = [
    "informatica_tic",
    "administracion_general",
    "sanidad",
    "educacion",
    "seguridad",
    "justicia",
    "hacienda",
    "ingenieria",
    "medio_ambiente",
    "obras_publicas",
    "trabajo_y_asuntos_sociales",
    "cultura",
    "estadistica_y_investigacion",
    "otros",
]

GRUPOS_VALIDOS = ["A1", "A2", "B", "C1", "C2", "AP", "no_especificado"]

TURNOS_VALIDOS = [
    "libre",
    "promocion_interna",
    "discapacidad",
    "mixto",
    "no_especificado",
]

PALABRAS_CLAVE_OPOSICION = [
    "plaza", "plazas", "convocatoria", "convoca", "turno", "oposicion",
    "oposición", "concurso-oposicion", "concurso-oposición", "proceso selectivo",
    "bases especificas", "bases específicas", "funcionario", "personal laboral",
]
