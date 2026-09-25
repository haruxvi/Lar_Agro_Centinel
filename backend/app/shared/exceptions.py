"""Exception hierarchy shared across modules."""

from __future__ import annotations


class DomainError(Exception):
    """Base de todas las excepciones de dominio.

    Los routers las traducen a HTTP; los servicios nunca lanzan
    HTTPException.
    """

    def as_dict(self) -> dict[str, object]:
        """Payload serializable para el detail de la respuesta.

        No incluir datos sensibles: este dict llega al cliente.
        """
        return {"error": type(self).__name__}
