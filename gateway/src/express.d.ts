import type { Account } from "./accounts.js";

declare global {
  namespace Express {
    interface Request {
      account?: Account;
      keyHash?: string;
      requestId?: string;
    }
  }
}

export {};
