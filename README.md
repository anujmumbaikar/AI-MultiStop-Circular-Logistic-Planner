# Logistics Planner

AI-powered logistics route optimizer that turns inbound Gmail requests into a routed pickup schedule, stores an audit trail in Google Sheets, and sends a confirmation reply back to the sender.

## What It Does

The project ingests unread Gmail messages, extracts structured pickup data with GPT-4o, geocodes pickup and delivery locations with OpenRouteService, optimizes the pickup order for a truck route, writes the results to Google Sheets, and sends an HTML reply to the original email thread.

## Workflow

The main workflow lives in [agent.ipynb](agent.ipynb). It is built as a LangGraph pipeline with these steps:

1. `gmail_trigger` - create a `REQ-YYYYMMDD-<6-char hash>` request id and check for duplicates.
2. `parser_agent` - use GPT-4o to extract sender info and pickup stops from the email body.
3. `save_email_logs_to_sheet` - save the raw email and parsed stops to Google Sheets.
4. `route_optimization` - geocode pickup and delivery addresses, fetch elevation, and optimize the route with ORS VROOM.
5. `collect_shipment_record` - build an HTML table fragment for the reply email.
6. `format_schedule` - build a plain-text schedule summary.
7. `merge_tasks` - wait for the parallel tasks to finish.
8. `ai_agent_reply` - generate the final HTML confirmation email.
9. `send_reply_to_gmail` - send the reply in the original Gmail thread.
10. `error_handler` - log failures to the error sheet.

## Repository Layout

- [agent.ipynb](agent.ipynb) - notebook that contains the full LangGraph pipeline and runtime entry point.
- [tools/gmail_tools.py](tools/gmail_tools.py) - Gmail polling and threaded reply helpers.
- [tools/sheets_tools.py](tools/sheets_tools.py) - Google Sheets logging helpers and duplicate detection.
- [tools/ors_tools.py](tools/ors_tools.py) - ORS geocoding, elevation, optimization, and distance matrix helpers.
- [auth_setup.py](auth_setup.py) - one-time Google OAuth setup that creates `credentials/token.json`.
- [requirements.txt](requirements.txt) - Python dependencies.

## Requirements

- Python 3.13 or compatible virtual environment
- Google OAuth credentials in `credentials/credentials.json`
- Google token file in `credentials/token.json`
- `OPENAI_API_KEY`
- `ORS_API_KEY`
- `GOOGLE_SHEET_ID`

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Add a `.env` file with the required environment variables.
4. Download your Google OAuth client JSON and save it as `credentials/credentials.json`.
5. Run `python auth_setup.py` once to create `credentials/token.json`.

## Environment Variables

The notebook and helper modules read these variables:

- `OPENAI_API_KEY` - OpenAI API key used by GPT-4o.
- `ORS_API_KEY` - OpenRouteService API key.
- `GMAIL_POLL_INTERVAL` - polling interval in seconds, default `60`.
- `GMAIL_QUERY` - Gmail search query, default `is:unread subject:Pickup Schedule`.
- `GOOGLE_TOKEN_PATH` - OAuth token path, default `credentials/token.json`.
- `GOOGLE_SHEET_ID` - Google Sheet id used for logs and route output.

## Google Sheets Tabs

The Sheets integration writes to these tabs:

- `email_log`
- `parsed_stops`
- `geocoded`
- `route_output`
- `error_log`

## Running The App

Open [agent.ipynb](agent.ipynb) and execute the cells in order. The last notebook cell starts the polling loop and continuously checks Gmail for new requests.

The helper scripts can also be run directly:

- `python auth_setup.py` - create the Google OAuth token.
- `python tools/ors_tools.py` - run ORS helper examples.

## Implementation Notes

- The route optimization uses `driving-hgv`, not a passenger car profile.
- The pipeline is built with LangGraph fan-out after parsing, then merges the parallel tasks before composing the reply.
- Duplicate requests are skipped by checking the generated request id against the email log.

## Future Todo
Work in progress:

1. Make the LangGraph structure more defined and easier to extend.
2. Add more detailed graph state fields for better tracing and debugging.
3. Improve route distance accuracy, since the current calculated distance is not fully reliable.
4. Save more detailed records to Google Sheets.
5. Support Gmail attachments such as `.csv` and `.xlsx` files.
6. For now, this is implemented in the notebook file; later it will be moved into a proper folder-based structure.