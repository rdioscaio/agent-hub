BEGIN;

CREATE TABLE IF NOT EXISTS automation_config_audit (
  id bigserial PRIMARY KEY,
  config_key text NOT NULL,
  operation text NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
  old_value jsonb,
  new_value jsonb,
  changed_at timestamptz NOT NULL DEFAULT now(),
  db_user text NOT NULL DEFAULT current_user,
  txid bigint NOT NULL DEFAULT txid_current(),
  application_name text,
  comment text
);

CREATE INDEX IF NOT EXISTS automation_config_audit_key_changed_idx
  ON automation_config_audit (config_key, changed_at DESC);

CREATE OR REPLACE FUNCTION audit_automation_config_changes()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO automation_config_audit (
    config_key,
    operation,
    old_value,
    new_value,
    changed_at,
    db_user,
    txid,
    application_name,
    comment
  )
  VALUES (
    COALESCE(NEW.key, OLD.key),
    TG_OP,
    CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.value ELSE NULL END,
    CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.value ELSE NULL END,
    now(),
    current_user,
    txid_current(),
    current_setting('application_name', true),
    NULLIF(current_setting('agent_hub.audit_comment', true), '')
  );

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_automation_config_audit ON automation_config;

CREATE TRIGGER trg_automation_config_audit
AFTER INSERT OR UPDATE OR DELETE ON automation_config
FOR EACH ROW
EXECUTE FUNCTION audit_automation_config_changes();

COMMIT;
