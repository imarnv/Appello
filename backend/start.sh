#!/bin/bash
# ─── Appello Bridge — Startup Script ──────────────────────────────────────
# Boots main.py (8000) and test_realtime_gemini.py (8086)
# ───────────────────────────────────────────────────────────────────────────

echo "⚙️ Booting backend servers..."

# Auto-activate virtual environment if it exists
if [ -f "./venv/bin/activate" ]; then
    echo "💡 Activating virtual environment (venv)..."
    source ./venv/bin/activate
elif [ -f "../venv/bin/activate" ]; then
    echo "💡 Activating virtual environment (venv)..."
    source ../venv/bin/activate
fi

# Start main.py (API + Exotel on port 8000)
PORT=8000 python3 -u main.py > main_server.log 2>&1 &
MAIN_PID=$!
echo "⏳ [BOOTING] main.py (API & Production) on port 8000 (PID: $MAIN_PID)..."

# Start test_realtime_gemini.py (Gemini Multimodal Live on 8086)
python3 -u test_realtime_gemini.py > gemini_live.log 2>&1 &
GEMINI_PID=$!
echo "⏳ [BOOTING] test_realtime_gemini.py (Gemini Live API) on port 8086 (PID: $GEMINI_PID)..."

# Wait a brief moment to check process stability
sleep 4

# Check main.py status
if kill -0 $MAIN_PID 2>/dev/null; then
    echo "🟢 [RUNNING] main.py successfully booted."
else
    echo "🔴 [ERROR] main.py failed to start! Check main_server.log for details:"
    cat main_server.log | tail -n 20
fi

# Check test_realtime_gemini.py status
if kill -0 $GEMINI_PID 2>/dev/null; then
    echo "🟢 [RUNNING] test_realtime_gemini.py successfully booted."
else
    echo "🔴 [ERROR] test_realtime_gemini.py failed to start! Check gemini_live.log for details:"
    cat gemini_live.log | tail -n 20
fi

# Keep process alive by waiting on the main FastAPI process
wait $MAIN_PID
