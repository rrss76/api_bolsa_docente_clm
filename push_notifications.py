#!/usr/bin/env python3
"""
push_notifications.py
----------------------
Envía una notificación push (Firebase Cloud Messaging) al topic
"actualizaciones" cuando el pipeline termina de actualizar la base de datos.
La app Flutter suscribe todos los dispositivos a ese mismo topic.

Variable de entorno necesaria:
  FIREBASE_SERVICE_ACCOUNT_JSON   Contenido JSON de la cuenta de servicio
                                   de Firebase (Project settings → Service
                                   accounts → Generate new private key).
"""

import json
import logging
import os

log = logging.getLogger("pipeline")

TOPIC = "actualizaciones"

_firebase_app = None


def _get_app():
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    creds_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        return None

    import firebase_admin
    from firebase_admin import credentials

    cred = credentials.Certificate(json.loads(creds_json))
    _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


def notificar_actualizacion(titulo: str, cuerpo: str) -> None:
    """Envía un push al topic 'actualizaciones'. Nunca lanza excepción:
    un fallo de notificación no debe tumbar el pipeline de datos."""
    try:
        app = _get_app()
        if app is None:
            log.warning("FIREBASE_SERVICE_ACCOUNT_JSON no configurado; push omitido.")
            return

        from firebase_admin import messaging

        mensaje = messaging.Message(
            notification=messaging.Notification(title=titulo, body=cuerpo),
            topic=TOPIC,
        )
        messaging.send(mensaje)
        log.info("✓ Notificación push enviada.")
    except Exception as e:
        log.error(f"✗ Error enviando notificación push: {e}")
