import type { Account } from "./accounts.js";

declare global {
  namespace Express {
    interface Request {
      account?: Account;
      accountId?: number;
      userEmail?: string | null;
      keyHash?: string;
      requestId?: string;
    }
  }
}

export {};
