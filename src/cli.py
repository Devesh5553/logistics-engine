import argparse
import os
import sys
from pathlib import Path
from sqlalchemy import inspect

from src.config import BASE_DIR, DATABASE_URL
from src.db import engine, init_db, drop_db, SessionLocal
from src.seeds.sample_data import seed_sample_data
from src.seeds.scale_data import seed_scale_data
from src.seeds.readme_generator import update_readme_file, generate_access_lines


def cmd_init_db(args):
    print("Initializing database tables...")
    init_db()
    print("Database tables initialized successfully.")


def cmd_seed(args):
    print("Initializing clean schema for sample data...")
    drop_db()
    init_db()
    session = SessionLocal()
    try:
        counts = seed_sample_data(session)
        print("\n=== Sample Data Seeded Successfully ===")
        for entity, count in counts.items():
            print(f"  - {entity.capitalize()}: {count}")

        readme_file = BASE_DIR / "README.md"
        if readme_file.exists():
            update_readme_file(str(readme_file), session)
            print(f"\nUpdated README access lines in: {readme_file}")
    finally:
        session.close()


def cmd_seed_scale(args):
    print(f"Running scale data generation ({args.jobs:,} jobs, {args.users:,} users)...")
    res = seed_scale_data(
        num_users=args.users,
        num_jobs=args.jobs,
        batch_size=args.batch_size,
        seed=args.seed
    )
    report_path = Path(res["report_file"])
    if report_path.exists():
        print("\n" + report_path.read_text(encoding="utf-8"))
    print(f"Full scale report saved to: {res['report_file']}\n")


def cmd_generate_readme(args):
    session = SessionLocal()
    try:
        readme_file = BASE_DIR / "README.md"
        if not readme_file.exists():
            print(f"README.md not found at {readme_file}, creating it...")
            readme_file.write_text("# Nagarkot Access Control Service\n\n", encoding="utf-8")
        update_readme_file(str(readme_file), session)
        print(f"Updated README access section in {readme_file}")
    finally:
        session.close()


def cmd_verify_schema(args):
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Found {len(tables)} tables in database:")
    for t in sorted(tables):
        cols = inspector.get_columns(t)
        indexes = inspector.get_indexes(t)
        idx_names = [f"{i['name']} ({', '.join(i['column_names'])})" for i in indexes]
        print(f"  • {t} ({len(cols)} cols, indexes: {', '.join(idx_names) if idx_names else 'none'})")


def cmd_serve(args):
    import uvicorn
    print(f"Starting NFPL Access Control Service API on http://{args.host}:{args.port} (Swagger docs at /docs)...")
    uvicorn.run("src.api.app:app", host=args.host, port=args.port, reload=args.reload)


def main():
    parser = argparse.ArgumentParser(description="NFPL Access Control Service Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db
    subparsers.add_parser("init-db", help="Create database tables")

    # seed
    subparsers.add_parser("seed", help="Seed small deliberate dataset (~300 jobs, 20 users, 5 branches)")

    # seed-scale
    p_scale = subparsers.add_parser("seed-scale", help="Seed scale benchmark dataset (200k jobs, 500 users)")
    p_scale.add_argument("--users", type=int, default=500, help="Number of users to seed (default: 500)")
    p_scale.add_argument("--jobs", type=int, default=200_000, help="Number of jobs to seed (default: 200,000)")
    p_scale.add_argument("--batch-size", type=int, default=20_000, help="Batch size for bulk insert")
    p_scale.add_argument("--seed", type=int, default=42, help="Random seed for repeatability")

    # generate-readme-access
    subparsers.add_parser("generate-readme-access", help="Generate plain-English access lines into README.md")

    # verify-schema
    subparsers.add_parser("verify-schema", help="Inspect database schema and indexes")

    # serve
    p_serve = subparsers.add_parser("serve", help="Run the FastAPI API server")
    p_serve.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"), help="Host address (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")), help="Port (default: 8000)")
    p_serve.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    commands = {
        "init-db": cmd_init_db,
        "seed": cmd_seed,
        "seed-scale": cmd_seed_scale,
        "generate-readme-access": cmd_generate_readme,
        "verify-schema": cmd_verify_schema,
        "serve": cmd_serve,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
