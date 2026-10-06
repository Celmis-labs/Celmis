import { createServer } from "node:http";
import { loadConfig } from "./config.js";
import { createInvoice } from "./invoices/createInvoice.js";

const config = loadConfig();

createServer(async (req, res) => {
  if (req.method === "POST" && req.url === "/invoices") {
    const invoice = await createInvoice("c-1", 1000);
    res.end(JSON.stringify(invoice));
    return;
  }
  res.statusCode = 404;
  res.end();
}).listen(config.port);
