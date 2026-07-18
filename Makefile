# Find My Trial — documented bootstrap / test / run commands (feedback P0 #2).
# A clean checkout can install, fetch data, test, benchmark, and run from these.
CORPUS_URL ?= https://github.com/PinkSheepDog/find-my-trial/releases/download/corpus-v1/trials.csv
PY ?= python3

.PHONY: setup corpus test benchmark run frontend clean

setup:            ## Create the backend venv and install deps
	cd backend && $(PY) -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

corpus:           ## Download the trial corpus (synthetic/public; not committed)
	mkdir -p backend/data && curl -fsSL -o backend/data/trials.csv "$(CORPUS_URL)" && wc -l backend/data/trials.csv

test:             ## Run the full test suite incl. the benchmark release gate
	cd backend && . .venv/bin/activate && python -m pytest -q

benchmark:        ## Print the synthetic-EHR scorecard
	cd backend && . .venv/bin/activate && python benchmark/run_benchmark.py

run:              ## Start the API (serves the built frontend if present)
	cd backend && . .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

frontend:         ## Build the React app into frontend/dist
	cd frontend && npm ci && npm run build

clean:            ## Remove caches and build artifacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf backend/.pytest_cache frontend/dist
