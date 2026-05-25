import os
import logging
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, before_log

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LENGTH = 4096


class TelegramClient:
    def __init__(self):
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _split_message(self, text: str) -> list[str]:
        """Divide el texto en partes de máximo 4096 caracteres respetando saltos de línea."""
        if len(text) <= MAX_TELEGRAM_LENGTH:
            return [text]

        parts = []
        lines = text.split("\n")
        current_part = []
        current_length = 0

        for line in lines:
            line_length = len(line) + 1  # +1 por el salto de línea
            if current_length + line_length > MAX_TELEGRAM_LENGTH and current_part:
                parts.append("\n".join(current_part))
                current_part = [line]
                current_length = line_length
            else:
                current_part.append(line)
                current_length += line_length

        if current_part:
            parts.append("\n".join(current_part))

        return parts

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def _send_single(self, text: str) -> None:
        """Envía una parte del mensaje a Telegram (lanza excepción si falla)."""
        response = requests.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=30,
        )
        response.raise_for_status()

    def send_message(self, text: str) -> bool:
        """Envía el texto al chat de Telegram, dividiéndolo si supera 4096 caracteres."""
        parts = self._split_message(text)
        logger.info("Enviando mensaje a Telegram en %d parte(s)...", len(parts))

        try:
            for i, part in enumerate(parts):
                self._send_single(part)
                if i < len(parts) - 1:
                    time.sleep(0.5)
            logger.info("Mensaje enviado correctamente a Telegram.")
            return True
        except Exception as e:
            logger.error("Error al enviar mensaje a Telegram: %s", e)
            return False
