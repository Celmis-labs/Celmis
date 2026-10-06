/** Configuration for the billing service, loaded once from the environment. */

export interface Config {
  databaseUrl: string;
  poolSize: number;
  stripeApiKey: string;
  port: number;
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`missing required environment variable ${name}`);
  }
  return value;
}

export function loadConfig(): Config {
  return {
    databaseUrl: required("DATABASE_URL"),
    poolSize: Number(process.env.DB_POOL_SIZE ?? "10"),
    stripeApiKey: required("STRIPE_API_KEY"),
    port: Number(process.env.PORT ?? "3000"),
  };
}
