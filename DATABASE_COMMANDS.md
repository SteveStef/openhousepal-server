# Database Commands Reference

This guide covers all the essential database commands for your Collections Software project.

## Environment Setup

### Activate Virtual Environment
```bash
source venv/bin/activate
```
**What it does**: Activates your Python virtual environment where all packages are installed.

### Install Dependencies
```bash
pip install -r requirements.txt
```
**What it does**: Installs all required Python packages including SQLAlchemy, Alembic, FastAPI, etc.

## Database Migration Commands

### Generate New Migration
```bash
python -m alembic revision --autogenerate -m "Description of changes"
```
**What it does**: 
- Compares your SQLAlchemy models with the current database schema
- Automatically generates a migration file with the differences
- Creates a new file in `alembic/versions/` with upgrade/downgrade functions

### Apply Migrations (Upgrade)
```bash
python -m alembic upgrade head
```
**What it does**: 
- Applies all pending migrations to bring database to latest version
- Creates tables, columns, indexes as defined in your models
- **Run this first time** to create your database structure

### Rollback Migration (Downgrade)
```bash
python -m alembic downgrade -1
```
**What it does**: Rolls back the last migration (useful if you made a mistake)

### Check Migration Status
```bash
python -m alembic current
```
**What it does**: Shows the current migration version of your database

### View Migration History
```bash
python -m alembic history
```
**What it does**: Lists all migrations and their status (applied or pending)

## SQLite Database Commands

### Open SQLite Command Line
```bash
sqlite3 collections.db
```
**What it does**: Opens interactive SQLite shell to run SQL commands directly

### List All Tables
```sql
.tables
```
**What it does**: Shows all tables in your database (users, collections, properties, etc.)

### Show Table Schema
```sql
.schema users
.schema collections
.schema properties
```
**What it does**: Displays the CREATE statement for a specific table

### View Table Data
```sql
SELECT * FROM users;
SELECT * FROM collections;
SELECT * FROM properties;
```
**What it does**: Shows all data in a specific table

### Count Records
```sql
SELECT COUNT(*) FROM users;
```
**What it does**: Shows how many records are in a table

### Exit SQLite Shell
```sql
.quit
```

## FastAPI Server Commands

### Start Development Server
```bash
python -m uvicorn app.main:app --reload
```
**What it does**: 
- Starts your FastAPI server on http://localhost:8000
- `--reload` automatically restarts server when code changes
- Access interactive docs at http://localhost:8000/docs

### Start Production Server
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
**What it does**: Starts server accessible from other machines (no auto-reload)

## Database File Management

### Backup Database
```bash
cp collections.db collections_backup_$(date +%Y%m%d).db
```
**What it does**: Creates a timestamped backup of your entire database

### Delete Database (Start Fresh)
```bash
rm collections.db
python -m alembic upgrade head
```
**What it does**: 
- Deletes the database file completely
- Runs migrations to recreate fresh database structure

### Check Database Size
```bash
ls -lh collections.db
```
**What it does**: Shows the size of your database file

## Useful Development Workflows

### Adding New Model/Table
1. Edit your model in `app/models/database.py`
2. Generate migration: `python -m alembic revision --autogenerate -m "Add new table"`
3. Apply migration: `python -m alembic upgrade head`
4. Restart server to use new model

### Reset Database During Development
```bash
rm collections.db
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```
**What it does**: Completely resets database to clean state with latest schema

### View Recent Database Changes
```bash
python -m alembic history --indicate-current -v
```
**What it does**: Shows detailed migration history with current version highlighted

## Testing Database

### Create Test User via API
```bash
curl -X POST "http://localhost:8000/auth/signup" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "testpass123",
    "first_name": "Test",
    "last_name": "User"
  }'
```

### Check if User was Created
```bash
sqlite3 collections.db "SELECT * FROM users;"
```

## Troubleshooting Commands

### Check Python Environment
```bash
which python
pip list | grep -E "(sqlalchemy|alembic|fastapi)"
```
**What it does**: Verifies you're using the right Python and have required packages

### Verify Database Connection
```bash
python -c "
import asyncio
from app.database import init_db
asyncio.run(init_db())
print('Database connection successful!')
"
```

### Check Migration Files
```bash
ls -la alembic/versions/
```
**What it does**: Lists all generated migration files

## Important Notes

- Always activate your virtual environment before running commands
- SQLite database is just a file (`collections.db`) - you can copy/delete it anytime
- Migrations are tracked in `alembic_version` table inside your database
- Use `--autogenerate` for model changes, manual migrations for data changes
- Always backup production databases before running migrations
