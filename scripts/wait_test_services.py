"""Wait only for this repository's loopback synthetic-test services."""
import time

import httpx
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text


def main():
    engine = create_engine('postgresql+psycopg://postgres@127.0.0.1:17654/inkwise_test', connect_args={'connect_timeout': 2})
    driver = GraphDatabase.driver('bolt://127.0.0.1:17687', auth=None, connection_timeout=2)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with engine.connect() as connection:
                    connection.execute(text('SELECT 1'))
                with driver.session() as session:
                    assert session.run('RETURN 1 AS ready').single()['ready'] == 1
                with httpx.Client(timeout=2, trust_env=False) as client:
                    client.get('http://127.0.0.1:18080/api/v2/heartbeat').raise_for_status()
                print('Synthetic PostgreSQL, Neo4j and ChromaDB ready')
                return
            except Exception:
                time.sleep(1)
        raise SystemExit('TEST_SERVICES_NOT_READY; no private details printed')
    finally:
        driver.close()
        engine.dispose()


if __name__ == '__main__':
    main()
