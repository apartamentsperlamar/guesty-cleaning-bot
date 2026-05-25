# 🧹 Guesty Cleaning Bot

Bot automático que envía diariamente las listas de limpiezas de los apartamentos NEPTÚ24 a la limpiadora vía Telegram, leyendo las **reservas** desde la API de Guesty y construyendo los turnos de limpieza a partir de los check-outs del día combinados con la siguiente reserva entrante.

## ¿Qué hace?

- **Cada día a las 08:00 (Madrid):** Envía las limpiezas programadas para hoy (check-outs de hoy + info del siguiente check-in)
- **Cada día a las 15:00 (Madrid):** Envía las limpiezas del día siguiente
- **Cada lunes a las 08:00 (Madrid):** Envía el resumen completo de la semana

## Lógica de negocio

El bot **no lee tareas de limpieza**. En su lugar:
1. Busca todas las reservas con check-out en la fecha indicada
2. Para cada una, busca la siguiente reserva en el mismo apartamento
3. Construye el "turno de limpieza" combinando ambas reservas:
   - De la reserva saliente: hora de check-out, número de huéspedes
   - De la reserva entrante: hora y fecha de check-in, número de huéspedes, notas

Una limpieza es **⚡ ALTA PRIORIDAD** cuando el check-in entrante es el mismo día que el check-out.

## Comandos del bot

La limpiadora puede escribir en el chat del bot para obtener información bajo demanda:

| Comando | Resultado |
|---------|-----------|
| `/dia` | Limpiezas programadas para hoy |
| `/mañana` | Limpiezas programadas para mañana |
| `/semana` | Resumen completo de la semana |

### Arrancar el listener desde GitHub Actions

1. Ve a **Actions → Guesty Cleaning Bot → Run workflow**
2. Selecciona el modo `listen`
3. El job `bot-listener` se ejecutará hasta 6 horas escuchando comandos

> **Nota:** El listener solo responde a mensajes del chat configurado en `TELEGRAM_CHAT_ID`.

## Estructura del proyecto

```
guesty-cleaning-bot/
├── .github/
│   └── workflows/
│       └── scheduler.yml
├── src/
│   ├── __init__.py
│   ├── guesty_client.py
│   ├── telegram_client.py
│   ├── message_formatter.py
│   ├── bot_listener.py
│   └── main.py
├── .env.example
├── requirements.txt
├── README.md
└── .gitignore
```

## Configuración

### GitHub Secrets requeridos

| Secret | Descripción |
|--------|-------------|
| `GUESTY_CLIENT_ID` | Client ID de Guesty Open API |
| `GUESTY_CLIENT_SECRET` | Client Secret de Guesty Open API |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | ID del grupo de Telegram |

### Variables de entorno locales

Copia `.env.example` como `.env` y rellena los valores reales.

## Ejecución local

```bash
pip install -r requirements.txt
cp .env.example .env
# Editar .env con las credenciales reales
python -m src.main daily_morning
python -m src.main daily_afternoon
python -m src.main weekly
python -m src.main listen    # escucha comandos en tiempo real
```

## Ejecución manual en GitHub Actions

Ve a **Actions → Guesty Cleaning Bot → Run workflow** y selecciona el modo deseado.

## Formato del mensaje diario (ejemplo)

```
🧹 LIMPIEZAS DE HOY — Martes, 22 de mayo de 2025
━━━━━━━━━━━━━━━━━━━━━

1. NEPTÚ24-3
⚡ ENTRADA HOY
🚪 Check-out: 11:00 (reserva saliente: 4 huéspedes)
🔑 Check-in: 16:00
👥 Huéspedes entrantes: 2 huéspedes + 1 bebé
📝 Alérgicos al gluten, dejar info en mesita
━━━━━━━━━━━━━━━━━━━━━

2. NEPTÚ24-1
🚪 Check-out: 11:00 (reserva saliente: 2 huéspedes)
🔑 Check-in: 15:00 el 24 de mayo
👥 Huéspedes entrantes: 3 huéspedes
━━━━━━━━━━━━━━━━━━━━━

Total: 2 limpiezas programadas
```

## Apartamentos

- NEPTÚ24-1
- NEPTÚ24-2
- NEPTÚ24-3
- NEPTÚ24-4
- NEPTÚ24-5

## Tecnologías

- Python 3.11
- Guesty Open API (reservas)
- Telegram Bot API
- GitHub Actions (scheduler)
- tenacity (reintentos automáticos)
- pytz (zona horaria Europe/Madrid)
