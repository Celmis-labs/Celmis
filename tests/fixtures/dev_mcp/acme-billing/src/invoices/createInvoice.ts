import { pool } from "../db/pool.js";

export interface Invoice {
  id: number;
  customerId: string;
  amountCents: number;
}

/** Insert an invoice row and return it. */
export async function createInvoice(customerId: string, amountCents: number): Promise<Invoice> {
  const result = await pool.query(
    "INSERT INTO invoices (customer_id, amount_cents) VALUES ($1, $2) RETURNING id",
    [customerId, amountCents],
  );
  return { id: result.rows[0].id, customerId, amountCents };
}
