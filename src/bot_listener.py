import os
import logging
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, before_log

logger = logging.getLogger(__name__)

AYUDA = (
    "ℹ️ Comandos disponibles:\n"
    "/dia — Limpiezas de hoy\n"
    "/siguiente — Limpiezas de mañana\n"
    "/semana — Resumen semanal"
)


class BotListener:
    """
    Escucha comandos entrantes en el chat de Telegram mediante long polling.
    Solo responde a mensajes del chat configurado en TELEGRAM_CHAT_ID.
    """

    POLL_TIMEOUT = 30  # segundos de long polling por petición

    def __init__(self, guesty, telegram, formatter):
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.guesty = guesty
        self.telegram = telegram
        self.formatter = formatter
        self.offset = 0

    # ------------------------------------------------------------------ #
    # Polling                                                              #
    # ------------------------------------------------------------------ #

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def _get_updates(self) -> list:
        """Llama a getUpdates con long polling y devuelve la lista de updates."""
        response = requests.get(
            f"{self.base_url}/getUpdates",
            params={"timeout": self.POLL_TIMEOUT, "offset": self.offset},
            timeout=self.POLL_TIMEOUT + 10,
        )
        response.raise_for_status()
        return response.json().get("result", [])

    def _ack(self, update_id: int) -> None:
        """Avanza el offset para que el update no se procese de nuevo."""
        self.offset = update_id + 1

    # ------------------------------------------------------------------ #
    # Procesado de mensajes                                                #
    # ------------------------------------------------------------------ #

    def _handle_update(self, update: dict) -> None:
        """Procesa un update individual."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            logger.warning("Mensaje de chat no autorizado ignorado: %s", chat_id)
            return

        text = (message.get("text") or "").strip().lower()
        # Ignorar el sufijo @botname si existe
        comando = text.split("@")[0]

        logger.info("Comando recibido: '%s'", comando)

        try:
            if comando == "/dia":
                self._cmd_dia()
            elif comando == "/siguiente":
                self._cmd_manana()
            elif comando == "/semana":
                self._cmd_semana()
            elif comando.startswith("/"):
                # Cualquier otro slash-command → ayuda
                self.telegram.send_message(AYUDA)
        except Exception as e:
            logger.error("Error ejecutando comando '%s': %s", comando, e)
            self.telegram.send_message(f"⚠️ Error ejecutando '{comando}': {e}")

    # ------------------------------------------------------------------ #
    # Comandos                                                             #
    # ------------------------------------------------------------------ #

    def _cmd_dia(self) -> None:
        from datetime import datetime
        import pytz
        MADRID = pytz.timezone("Europe/Madrid")
        from src.main import run_daily_morning
        logger.info("Ejecutando /dia...")
        run_daily_morning(self.guesty, self.telegram, self.formatter)

    def _cmd_manana(self) -> None:
        from src.main import run_daily_afternoon
        logger.info("Ejecutando /mañana...")
        run_daily_afternoon(self.guesty, self.telegram, self.formatter)

    def _cmd_semana(self) -> None:
        from src.main import run_weekly
        logger.info("Ejecutando /semana...")
        run_weekly(self.guesty, self.telegram, self.formatter)

    # ------------------------------------------------------------------ #
    # Bucle principal                                                      #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Arranca el bucle de escucha. Se recupera automáticamente de errores de red."""
        logger.info("Bot listener iniciado. Escuchando comandos en chat %s...", self.chat_id)
        while True:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
                    self._ack(update["update_id"])
            except Exception as e:
                logger.error("Error en el bucle de polling: %s. Reintentando en 5s...", e)
                time.sleep(5)
