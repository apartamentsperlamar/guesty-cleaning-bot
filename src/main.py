import os
import sys
import logging
import traceback
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

load_dotenv()

from src.guesty_client import GuestyClient
from src.telegram_client import TelegramClient
from src.message_formatter import MessageFormatter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

MADRID = pytz.timezone("Europe/Madrid")
MODOS_VALIDOS = ("daily_morning", "daily_afternoon", "weekly")


def run_daily_morning(guesty: GuestyClient, telegram: TelegramClient, formatter: MessageFormatter):
    """Envía las limpiezas de hoy (ejecución de las 08:00)."""
    hoy = datetime.now(MADRID).strftime("%Y-%m-%d")
    logger.info("Modo: daily_morning | Fecha: %s", hoy)

    slots = guesty.build_cleaning_slots(hoy)
    logger.info("Turnos de limpieza encontrados para hoy: %d", len(slots))

    mensaje = formatter.format_daily_message(slots, hoy, "hoy")
    exito = telegram.send_message(mensaje)
    logger.info("Resultado del envío: %s", "OK" if exito else "FALLIDO")


def run_daily_afternoon(guesty: GuestyClient, telegram: TelegramClient, formatter: MessageFormatter):
    """Envía las limpiezas de mañana (ejecución de las 15:00)."""
    manana = (datetime.now(MADRID) + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Modo: daily_afternoon | Fecha objetivo: %s", manana)

    slots = guesty.build_cleaning_slots(manana)
    logger.info("Turnos de limpieza encontrados para mañana: %d", len(slots))

    mensaje = formatter.format_daily_message(slots, manana, "mañana")
    exito = telegram.send_message(mensaje)
    logger.info("Resultado del envío: %s", "OK" if exito else "FALLIDO")


def run_weekly(guesty: GuestyClient, telegram: TelegramClient, formatter: MessageFormatter):
    """Envía el resumen semanal de limpiezas (ejecución de los lunes a las 08:00)."""
    hoy = datetime.now(MADRID)
    # Calcular el lunes de la semana actual
    lunes = hoy - timedelta(days=hoy.weekday())
    dias = [(lunes + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    logger.info("Modo: weekly | Semana del %s al %s", dias[0], dias[6])

    # Usar rango para optimizar llamadas a la API
    start_date = dias[0]
    end_date = dias[6]

    try:
        # Intentar obtener todas las reservas de la semana en una sola llamada
        todas = guesty.get_reservations_in_range(start_date, end_date)
        logger.info("Reservas de la semana obtenidas: %d", len(todas))

        # Agrupar por día de check-out y construir slots
        from src.guesty_client import _parse_utc_to_madrid, _resolve_time, _extract_guests, _extract_notes

        slots_por_dia: dict[str, list] = {d: [] for d in dias}

        # Indexar por listing_id para buscar siguiente reserva
        from collections import defaultdict
        reservas_por_listing: dict[str, list] = defaultdict(list)
        for r in todas:
            lid = r.get("listingId") or r.get("listing", {}).get("_id") or ""
            if lid:
                reservas_por_listing[lid].append(r)

        for r in todas:
            checkout_time_w, checkout_date_w = _resolve_time(
                r.get("plannedDeparture"), r.get("checkOut")
            )
            if not checkout_date_w:
                continue
            dia_str = checkout_date_w
            if dia_str not in slots_por_dia:
                continue

            listing_id = r.get("listingId") or r.get("listing", {}).get("_id") or ""
            listing = guesty.get_listing(listing_id) if listing_id else None
            listing_name = (
                (listing.get("nickname") or listing.get("name")) if listing else None
            ) or listing_id or "Apartamento desconocido"

            out_adults, out_children, out_infants = _extract_guests(r)

            # Buscar la siguiente reserva en la misma propiedad (con detalles completos)
            next_res_basic = guesty.get_next_reservation(listing_id, dia_str) if listing_id else None

            if next_res_basic:
                next_id = next_res_basic.get("_id")
                next_res = guesty.get_reservation_detail(next_id) if next_id else next_res_basic
                checkin_time, checkin_date = _resolve_time(
                    next_res.get("plannedArrival"), next_res.get("checkIn")
                )
                checkin_is_today = checkin_date == dia_str if checkin_date else False
                in_adults, in_children, in_infants = _extract_guests(next_res)
                incoming_notes = _extract_notes(next_res)

                slot = {
                    "listing_id": listing_id,
                    "listing_name": listing_name,
                    "checkout_reservation_id": r.get("_id", ""),
                    "checkout_time": checkout_time_w,
                    "checkout_date": dia_str,
                    "outgoing_adults": out_adults,
                    "outgoing_children": out_children,
                    "outgoing_infants": out_infants,
                    "has_next_reservation": True,
                    "checkin_reservation_id": next_res_basic.get("_id"),
                    "checkin_time": checkin_time,
                    "checkin_date": checkin_date,
                    "checkin_is_today": checkin_is_today,
                    "incoming_adults": in_adults,
                    "incoming_children": in_children,
                    "incoming_infants": in_infants,
                    "incoming_notes": incoming_notes,
                    "high_priority": checkin_is_today,
                }
            else:
                slot = {
                    "listing_id": listing_id,
                    "listing_name": listing_name,
                    "checkout_reservation_id": r.get("_id", ""),
                    "checkout_time": checkout_time_w,
                    "checkout_date": dia_str,
                    "outgoing_adults": out_adults,
                    "outgoing_children": out_children,
                    "outgoing_infants": out_infants,
                    "has_next_reservation": False,
                    "checkin_reservation_id": None,
                    "checkin_time": None,
                    "checkin_date": None,
                    "checkin_is_today": False,
                    "incoming_adults": None,
                    "incoming_children": None,
                    "incoming_infants": None,
                    "incoming_notes": None,
                    "high_priority": False,
                }

            slots_por_dia[dia_str].append(slot)

    except Exception:
        # Fallback: llamar día a día si el rango falla
        logger.warning("Fallo en llamada por rango. Usando llamadas individuales por día.")
        slots_por_dia = {}
        for dia_str in dias:
            slots_por_dia[dia_str] = guesty.build_cleaning_slots(dia_str)

    total = sum(len(v) for v in slots_por_dia.values())
    logger.info("Total de turnos de limpieza esta semana: %d", total)

    mensaje = formatter.format_weekly_message(slots_por_dia)
    exito = telegram.send_message(mensaje)
    logger.info("Resultado del envío: %s", "OK" if exito else "FALLIDO")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODOS_VALIDOS:
        logger.error(
            "Uso: python -m src.main <modo>  |  Modos válidos: %s",
            ", ".join(MODOS_VALIDOS),
        )
        sys.exit(1)

    modo = sys.argv[1]
    guesty = GuestyClient()
    telegram = TelegramClient()
    formatter = MessageFormatter()

    try:
        if modo == "daily_morning":
            run_daily_morning(guesty, telegram, formatter)
        elif modo == "daily_afternoon":
            run_daily_afternoon(guesty, telegram, formatter)
        elif modo == "weekly":
            run_weekly(guesty, telegram, formatter)
    except Exception as e:
        logger.error("Error no controlado en modo '%s':\n%s", modo, traceback.format_exc())
        alerta = (
            f"⚠️ ERROR EN EL BOT DE LIMPIEZAS\n\n"
            f"Modo: {modo}\n"
            f"Error: {e}\n\n"
            f"Revisa los logs de GitHub Actions."
        )
        try:
            telegram.send_message(alerta)
        except Exception:
            logger.error("No se pudo enviar la alerta de error a Telegram.")
        sys.exit(1)


if __name__ == "__main__":
    main()
