# RealRAG

A Retrieval-Augmented Generation (RAG) system for querying RMS codebase knowledge.

## Prerequisites

- **Python 3.11** — [Download here](https://www.python.org/downloads/)
- **Anthropic API key** — [Get one here](https://console.anthropic.com/)
- **OpenAI API key** — [Get one here](https://platform.openai.com/api-keys)

## Quick Setup (First Time)

**1. Clone the repo**
```bash
git clone https://github.com/santhoshpilligundla/REALRAG.git
cd REALRAG
```

**2. Run the setup script**
```
setup.bat
```
This will:
- Create the Python virtual environment (`.venv`)
- Install all dependencies from `requirements.txt`
- Create your `.env` file from the template
- Initialize the database

**3. Add your API keys**

Open `.env` and fill in:
```
ANTHROPIC_API_KEY=your-key-here
OPENAI_API_KEY=your-key-here
```

**4. Download pre-built indexes (required for Chat to work)**

Download the FAISS indexes from OneDrive:
👉 [Download realrag-storage.zip](https://realpage-my.sharepoint.com/:u:/p/santhosh_pilligundla/IQCJ7fythoHeQJ9qtAq5_6f1AXRaFGzrDu-jBq2Y92TrBFk?email=santhosh.pilligundla%40RealPage.com&e=CNE0QI)

Extract the zip — you should get a `faiss` folder. Place it inside `storage/`:
```
REALRAG/
└── storage/
    └── faiss/       ← extracted here
```

**5. Run the app**
```
run_realrag.bat
```

The app opens at `http://localhost:8501`

---

## Running the App (After Setup)

Every time you want to start the app:
```
run_realrag.bat
```

---

## Project Structure

```
RealRAG/
├── frontend/          # Streamlit UI
├── lib/               # Core library (chat, retrieval, embedder, etc.)
├── scripts/           # Pipeline and diagnostic scripts
├── data/              # Glossaries and config
├── db/                # Database schema
├── docs/              # Business docs and HTML reports
├── storage/           # Auto-created: database + FAISS indexes (gitignored)
├── .env.example       # Template for environment variables
├── setup.bat          # First-time setup script
└── run_realrag.bat    # Launch the app
```

---

## Troubleshooting

**App won't start — database error**
```
del storage\pg-data\postmaster.pid
run_realrag.bat
```

**Missing packages**
```
.venv\Scripts\pip install -r requirements.txt
```

**Python not found**
Install Python 3.11 from https://python.org — check "Add to PATH" during install.
