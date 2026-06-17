@echo off
cd /d c:\codebase\RealRAG
if exist storage\pg-data\postmaster.pid del storage\pg-data\postmaster.pid
.venv\Scripts\streamlit run frontend\streamlit_app.py
