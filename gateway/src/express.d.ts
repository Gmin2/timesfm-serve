declare global {
  namespace Express {
    interface Request {
      tenant?: string;
      keyHash?: string;
      requestId?: string;
    }
  }
}

export {};
