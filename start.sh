#!/bin/bash
# Run DB migrations
python -c "import server; server.init_db(); server.migrate_db()"

# Start server
uvicorn server:app --host 0.0.0.0 --port $PORT
