# Ask your data — Text-to-SQL Data Analyst

Sign up, log in, and chat with a sample e-commerce database in plain English.
The agent writes SQL, runs it read-only, fixes its own errors, then explains the result with a table and chart.

**Features:** signup/login (scrypt-hashed passwords, signed session tokens) · per-user saved chat history · clear chat ·
LangGraph agent with self-repair · SELECT-only sandbox · schema explorer · syntax-highlighted SQL · auto charts

## Run
```bash
pip install -r requirements.txt
cd backend
copy .env.example .env      # Windows  (Mac/Linux: cp .env.example .env)
```
Edit `backend/.env` (one setting per line, no quotes):
```
GROQ_API_KEY=gsk_your_key
GROQ_MODEL=openai/gpt-oss-120b
APP_SECRET=any-long-random-string
```
Then:
```bash
python -m uvicorn main:app --reload --port 8080
```
Open http://localhost:8080 and create an account. **Restart the server after editing `.env`.**

## Docker
```bash
docker build -t sql-analyst . && docker run -p 8000:8000 --env-file backend/.env sql-analyst
```

## Ideas to extend
- Postgres/Supabase with a read-only role
- CSV upload to query your own data
- Stream agent steps over SSE
- Eval set with execution-accuracy scoring
