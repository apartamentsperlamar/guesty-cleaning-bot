from datetime import datetime, date, timedelta

import pytz

MADRID = pytz.timezone("Europe/Madrid")

DIAS = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}

MESES = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}

SEP = "━━━━━━━━━━━━━━━━━━━━━"


def _fecha_larga(date_obj: date) -> str:
    """Devuelve una fecha en formato 'Martes, 22 de mayo de 2025'."""
    return f"{DIAS[date_obj.weekday()]}, {date_obj.day} de {MESES[date_obj.month]} de {date_obj.year}"


def _fecha_corta(date_str: str | None) -> str | None:
    """Convierte 'YYYY-MM-DD' en 'DD de mes' (ej: '24 de mayo')."""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(date_str)
        return f"{d.day} de {MESES[d.month]}"
    except ValueError:
        return date_str


def _formato_huespedes(adults: int | None, children: int | None, infants: int | None) -> str:
    """Devuelve texto legible con desglose de adultos, niños y bebés."""
    if adults is None:
        return "Sin datos"
    parts = []
    if adults:
        parts.append(f"{adults} adulto{'s' if adults != 1 else ''}")
    if children:
        parts.append(f"{children} niño{'s' if children != 1 else ''}")
    if infants:
        parts.append(f"{infants} bebé{'s' if infants != 1 else ''}")
    return " + ".join(parts) if parts else "Sin datos"


def _sort_key(slot: dict):
    """Clave de ordenación: alta prioridad primero, luego por hora de checkout."""
    priority = 0 if slot.get("high_priority") else 1
    time_str = slot.get("checkout_time") or "99:99"
    return (priority, time_str)


class MessageFormatter:

    def format_daily_message(self, cleaning_slots: list, date_str: str, period: str) -> str:
        """Formatea el mensaje diario de limpiezas."""
        date_obj = date.fromisoformat(date_str)
        fecha_larga = _fecha_larga(date_obj)
        period_upper = period.upper()

        if not cleaning_slots:
            return f"✅ No hay limpiezas programadas para {period} ({fecha_larga})."

        slots_ordenados = sorted(cleaning_slots, key=_sort_key)

        lines = [
            f"🧹 LIMPIEZAS DE {period_upper} — {fecha_larga}",
            SEP,
            "",
        ]

        for i, slot in enumerate(slots_ordenados, start=1):
            nombre = slot.get("listing_name", "Apartamento")
            lines.append(f"{i}. <b>{nombre}</b>")

            if slot.get("high_priority"):
                lines.append("⚡ <b>ENTRADA HOY</b>")

            # Línea de check-out
            checkout_time = slot.get("checkout_time") or "?"
            lines.append(f"🚪 Check-out: {checkout_time}")

            # Línea de check-in
            if slot.get("has_next_reservation"):
                checkin_time = slot.get("checkin_time") or "?"
                if slot.get("checkin_is_today"):
                    lines.append(f"🔑 Check-in: {checkin_time}")
                else:
                    fecha_c = _fecha_corta(slot.get("checkin_date"))
                    lines.append(f"🔑 Check-in: {checkin_time} el {fecha_c}")
            else:
                lines.append("🔑 Sin reserva siguiente")

            # Línea de huéspedes entrantes
            in_adults = slot.get("incoming_adults")
            in_children = slot.get("incoming_children")
            in_infants = slot.get("incoming_infants")
            entrantes_txt = _formato_huespedes(in_adults, in_children, in_infants)
            lines.append(f"👥 Huéspedes entrantes: {entrantes_txt}")

            # Notas (máx 200 caracteres)
            notas = slot.get("incoming_notes")
            if notas:
                notas_truncadas = notas[:200] + ("..." if len(notas) > 200 else "")
                lines.append(f"📝 <i>{notas_truncadas}</i>")

            lines.append(SEP)
            lines.append("")

        n = len(slots_ordenados)
        lines.append(f"Total: {n} limpieza{'s' if n != 1 else ''} programada{'s' if n != 1 else ''}")

        return "\n".join(lines)

    def format_weekly_message(self, slots_by_day: dict) -> str:
        """Formatea el resumen semanal de limpiezas (lunes a domingo)."""
        # Calcular el lunes de la semana basándose en las claves disponibles o en hoy
        if slots_by_day:
            primera_fecha = date.fromisoformat(sorted(slots_by_day.keys())[0])
            lunes = primera_fecha - timedelta(days=primera_fecha.weekday())
        else:
            hoy = datetime.now(MADRID).date()
            lunes = hoy - timedelta(days=hoy.weekday())

        domingo = lunes + timedelta(days=6)

        inicio_txt = f"{lunes.day} de {MESES[lunes.month]}"
        fin_txt = f"{domingo.day} de {MESES[domingo.month]} de {domingo.year}"

        total_limpiezas = sum(len(v) for v in slots_by_day.values())

        lines = [
            "📅 RESUMEN SEMANAL DE LIMPIEZAS",
            f"Semana del {inicio_txt} al {fin_txt}",
            SEP,
            "",
        ]

        for offset in range(7):
            dia = lunes + timedelta(days=offset)
            dia_str = dia.strftime("%Y-%m-%d")
            dia_nombre = DIAS[dia.weekday()].upper()
            dia_corto = f"{dia.day} de {MESES[dia.month]}"

            lines.append(f"📆 {dia_nombre}, {dia_corto}:")

            slots_dia = slots_by_day.get(dia_str, [])
            if not slots_dia:
                lines.append("• Sin limpiezas")
            else:
                for slot in sorted(slots_dia, key=_sort_key):
                    nombre = slot.get("listing_name", "Apartamento")
                    checkout_t = slot.get("checkout_time") or "?"

                    if slot.get("has_next_reservation"):
                        checkin_t = slot.get("checkin_time") or "?"
                        if slot.get("checkin_is_today"):
                            checkin_txt = checkin_t
                        else:
                            checkin_d = slot.get("checkin_date")
                            if checkin_d:
                                d = date.fromisoformat(checkin_d)
                                checkin_txt = f"{checkin_t} el {d.day}/{d.month:02d}"
                            else:
                                checkin_txt = checkin_t
                    else:
                        checkin_txt = "Sin reserva"

                    in_adults = slot.get("incoming_adults")
                    in_children = slot.get("incoming_children")
                    in_infants = slot.get("incoming_infants")
                    entrantes_txt = _formato_huespedes(in_adults, in_children, in_infants)

                    lines.append(
                        f"• {nombre} — Checkout: {checkout_t} | Checkin: {checkin_txt} | Entrantes: {entrantes_txt}"
                    )

            lines.append("")

        lines.append(SEP)
        if total_limpiezas == 0:
            lines.append("✅ Sin limpiezas esta semana.")
        else:
            lines.append(f"TOTAL SEMANAL: {total_limpiezas} limpieza{'s' if total_limpiezas != 1 else ''}")

        return "\n".join(lines)
