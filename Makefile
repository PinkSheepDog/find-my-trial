# Find My Trial — documented bootstrap / test / run commands (feedback P0 #2).
# A clean checkout can install, fetch data, test, benchmark, and run from these.
CORPUS_URL ?= https://github.com/PinkSheepDog/find-my-trial/releases/download/corpus-v1/trials.csv
# Expected digest of the corpus-v1 asset. `make corpus` verifies the download so a
# swapped release asset or a truncated transfer fails loudly instead of being indexed.
CORPUS_SHA256 ?= 68c28b506e1d9abfd67a0e0069e59ac1f1d148a18c30e8901cd79cbe56ecde0c
PY ?= python3

.PHONY: setup corpus test benchmark run frontend clean

setup:            ## Create the backend venv and install deps
	cd backend && $(PY) -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

corpus:           ## Download + verify the trial corpus (public 10k SAMPLE; not committed)
	mkdir -p backend/data
	curl -fsSL -o backend/data/trials.csv.part "$(CORPUS_URL)"
	@echo "$(CORPUS_SHA256)  backend/data/trials.csv.part" | shasum -a 256 -c - \
	  || (rm -f backend/data/trials.csv.part; \
	      echo "ERROR: corpus failed checksum verification; refusing to install it."; exit 1)
	mv backend/data/trials.csv.part backend/data/trials.csv
	@echo "Corpus verified. NOTE: this is a 10,000-row sample, not the full ~555k registry."

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
