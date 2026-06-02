"""
semantic_router.py — Capa de enrutamiento semántico para VIERNES 2.0.

Pipeline de 2 etapas (sin modelos externos, solo stdlib):
  Stage 1 (<1ms):  keyword matching exacto/substring  → brain._detect_intent
  Stage 2 (<5ms):  TF-IDF coseno contra ejemplos de cada intent → SemanticRouter.route()
  Stage 3:         retorna None → fallback a _llm_classify_intent en brain.py

No requiere sklearn ni sentence-transformers. Usa math + Counter de stdlib.
"""

import logging
import math
import re
import unicodedata
from collections import Counter
from typing import Optional

logger = logging.getLogger("jarvis.router")


# ---------------------------------------------------------------------------
# Sinónimos / expansión léxica para palabras de acción en español
# Aumenta el recall sin añadir keywords al INTENT_MAP
# ---------------------------------------------------------------------------
_SYNONYMS: dict[str, list[str]] = {
    # Infinitivos + conjugaciones comunes (presente + imperativo)
    "abrir":    ["abre", "abrir", "lanzar", "lanza", "ejecutar", "ejecuta",
                 "iniciar", "inicia", "arrancar", "arranca", "mostrar", "muestra"],
    "cerrar":   ["cierra", "cerrar", "terminar", "termina", "salir",
                 "quitar", "quita", "matar", "mata"],
    "subir":    ["sube", "subir", "aumentar", "aumenta", "incrementar", "elevar"],
    "bajar":    ["baja", "bajar", "reducir", "reduce", "disminuir", "decrementar"],
    "poner":    ["pon", "poner", "colocar", "coloca", "activar", "activa", "encender"],
    "quitar":   ["quita", "quitar", "desactivar", "desactiva", "apagar",
                 "apaga", "eliminar", "elimina"],
    "buscar":   ["busca", "buscar", "encontrar", "encuentra", "localizar", "googlear"],
    "mostrar":  ["muestra", "mostrar", "ver", "ensenar", "visualizar", "listar", "lista"],
    "crear":    ["crea", "crear", "hacer", "haz", "generar", "genera", "nuevo"],
    "mover":    ["mueve", "mover", "trasladar", "traslada", "desplazar", "llevar"],
    "copiar":   ["copia", "copiar", "duplicar", "duplica", "clonar"],
    "borrar":   ["borra", "borrar", "eliminar", "elimina", "suprimir", "vaciar"],
    "pausar":   ["pausa", "pausar", "detener", "detiene", "parar", "para", "frenar"],
    "reanudar": ["reanuda", "reanudar", "continuar", "continua", "resumir", "seguir"],
    "bloquear": ["bloquea", "bloquear", "cerrar sesion", "cierra sesion"],
    "capturar": ["captura", "capturar", "foto", "imagen", "screenshot"],
    "silenciar":["silencia", "silenciar", "mutear", "mutea", "callar", "calla"],
}

# Inverso: alias → término canónico
_SYNONYM_MAP: dict[str, str] = {}
for _canonical, _aliases in _SYNONYMS.items():
    for _alias in _aliases:
        _SYNONYM_MAP[_alias] = _canonical


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase + quitar acentos + eliminar puntuación."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokenize(text: str, expand_synonyms: bool = False) -> list[str]:
    """Tokeniza y opcionalmente expande sinónimos al término canónico."""
    tokens = _normalize(text).split()
    if not expand_synonyms:
        return tokens
    return [_SYNONYM_MAP.get(tok, tok) for tok in tokens]


# ---------------------------------------------------------------------------
# SemanticRouter
# ---------------------------------------------------------------------------

class SemanticRouter:
    """
    Enrutador semántico TF-IDF coseno.

    Uso:
        router = SemanticRouter(brain.INTENT_MAP)
        intent, score = router.route("ponme algo más fuerte")
        # → ("volume_up", 0.41)
    """

    # Umbral mínimo de similitud coseno para aceptar la ruta
    THRESHOLD = 0.28

    def __init__(self, intent_map: dict[str, list[str]]) -> None:
        self._intent_map = intent_map
        self._vocab: dict[str, int] = {}
        self._idf: list[float] = []
        self._intent_vecs: dict[str, list[float]] = {}
        self._build_index()

    # ------------------------------------------------------------------
    # Construcción del índice TF-IDF
    # ------------------------------------------------------------------

    def _build_index(self) -> None:
        # Un "documento" por intent: todos sus keywords unidos
        docs: list[tuple[str, list[str]]] = []
        for intent, keywords in self._intent_map.items():
            tokens: list[str] = []
            for kw in keywords:
                tokens.extend(_tokenize(kw, expand_synonyms=True))
            docs.append((intent, tokens))

        N = len(docs)

        # Vocabulario global
        all_tokens: set[str] = set()
        for _, tokens in docs:
            all_tokens.update(tokens)
        self._vocab = {tok: i for i, tok in enumerate(sorted(all_tokens))}
        V = len(self._vocab)

        # Document Frequency
        df = [0] * V
        for _, tokens in docs:
            for tok in set(tokens):
                if tok in self._vocab:
                    df[self._vocab[tok]] += 1

        # IDF suavizado (Robertson / scikit-learn style)
        self._idf = [math.log((N + 1) / (d + 1)) + 1.0 for d in df]

        # Vector TF-IDF normalizado (L2) por intent
        for intent, tokens in docs:
            tf = Counter(tokens)
            total = len(tokens) or 1
            vec = [0.0] * V
            for tok, count in tf.items():
                if tok in self._vocab:
                    idx = self._vocab[tok]
                    vec[idx] = (count / total) * self._idf[idx]
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            self._intent_vecs[intent] = [x / norm for x in vec]

        logger.info(
            f"SemanticRouter listo: {N} intents, {V} tokens, "
            f"umbral={self.THRESHOLD}"
        )

    # ------------------------------------------------------------------
    # Vectorización de query
    # ------------------------------------------------------------------

    def _vectorize_query(self, tokens: list[str]) -> list[float]:
        """TF-IDF L2-normalizado para una lista de tokens de consulta."""
        V = len(self._vocab)
        tf = Counter(tokens)
        total = len(tokens) or 1
        vec = [0.0] * V
        for tok, count in tf.items():
            if tok in self._vocab:
                idx = self._vocab[tok]
                vec[idx] = (count / total) * self._idf[idx]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def route(self, text: str) -> tuple[Optional[str], float]:
        """
        Mapea texto libre al intent más probable.

        Returns:
            (intent_name, confidence)  si score >= THRESHOLD
            (None, best_score)         si ningún intent supera el umbral
        """
        tokens = _tokenize(text, expand_synonyms=True)
        if not tokens:
            return None, 0.0

        query_vec = self._vectorize_query(tokens)
        query_set = set(tokens)

        best_intent: Optional[str] = None
        best_score: float = 0.0

        for intent, intent_vec in self._intent_vecs.items():
            # Similitud coseno (vectores ya normalizados → producto punto)
            score = sum(q * d for q, d in zip(query_vec, intent_vec))

            # Penalización suave si no hay solapamiento léxico directo
            intent_tokens = set(_tokenize(
                " ".join(self._intent_map[intent]), expand_synonyms=True
            ))
            if not query_set.intersection(intent_tokens):
                score *= 0.6

            if score > best_score:
                best_score = score
                best_intent = intent

        if best_score >= self.THRESHOLD:
            logger.info(
                f"SemanticRouter → '{best_intent}' "
                f"(coseno={best_score:.3f})"
            )
            return best_intent, best_score

        logger.debug(
            f"SemanticRouter: sin ruta confiable "
            f"(mejor='{best_intent}' @ {best_score:.3f})"
        )
        return None, best_score
