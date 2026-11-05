// Generated from testfixtures/path-policy-cases.json; do not edit.
export const pathPolicyCases = [
  {
    "path": "tokens.ts",
    "denied": false
  },
  {
    "path": "secret.md",
    "denied": false
  },
  {
    "path": "credentials.py",
    "denied": false
  },
  {
    "path": "src/tokenizer.ts",
    "denied": false
  },
  {
    "path": "docs/secret.md",
    "denied": false
  },
  {
    "path": "node_modules/example/index.js",
    "denied": false
  },
  {
    "path": ".env",
    "denied": true
  },
  {
    "path": "config/.ENV.local",
    "denied": true
  },
  {
    "path": ".environment.ts",
    "denied": false
  },
  {
    "path": ".envrc",
    "denied": false
  },
  {
    "path": "auth.json",
    "denied": true
  },
  {
    "path": "config/credentials.json",
    "denied": true
  },
  {
    "path": "keys/private.key",
    "denied": true
  },
  {
    "path": "keys/CERT.PEM",
    "denied": true
  },
  {
    "path": ".ssh/id_ed25519",
    "denied": true
  },
  {
    "path": ".git/config",
    "denied": true
  },
  {
    "path": ".aws/config",
    "denied": true
  },
  {
    "path": ".claude/settings.json",
    "denied": true
  },
  {
    "path": "token/module.py",
    "denied": true
  },
  {
    "path": "config/.env.local/file.txt",
    "denied": true
  },
  {
    "path": "keys/private.key/info.txt",
    "denied": true
  }
] as const
