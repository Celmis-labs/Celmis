import { Pool } from "pg";
import { loadConfig } from "../config.js";

/** A single shared pg Pool; the connection string comes from DATABASE_URL. */
export function createPool(): Pool {
  const config = loadConfig();
  return new Pool({
    connectionString: config.databaseUrl,
    max: config.poolSize,
    idleTimeoutMillis: 30_000,
  });
}

export const pool = createPool();
