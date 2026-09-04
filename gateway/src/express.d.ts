declare global {
  namespace Express {
    interface Request {
      tenant?: string;
    }
  }
}

export {};
