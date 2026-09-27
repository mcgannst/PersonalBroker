-- Roles and schema of the throwaway smoke database (docker-compose.smoke.yml), set up like trader_dev:
-- the owner role owns schema `trader` and runs the migrations; the app role can only read and write rows
-- (default privileges cover the tables and sequences the owner creates later). The passwords protect
-- nothing: the database exists for one smoke run and listens on the Mac's 127.0.0.1 only.
CREATE ROLE trader_smoke_owner LOGIN PASSWORD 'smoke-owner';
CREATE ROLE trader_smoke_app LOGIN PASSWORD 'smoke-app';

GRANT CONNECT, CREATE ON DATABASE trader_smoke TO trader_smoke_owner;
GRANT CONNECT ON DATABASE trader_smoke TO trader_smoke_app;

CREATE SCHEMA trader AUTHORIZATION trader_smoke_owner;
GRANT USAGE ON SCHEMA trader TO trader_smoke_app;

ALTER DEFAULT PRIVILEGES FOR ROLE trader_smoke_owner IN SCHEMA trader
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trader_smoke_app;
ALTER DEFAULT PRIVILEGES FOR ROLE trader_smoke_owner IN SCHEMA trader
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO trader_smoke_app;
