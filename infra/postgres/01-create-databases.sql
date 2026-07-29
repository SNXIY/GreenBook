SELECT format(
  'CREATE DATABASE content_moderation OWNER %I',
  current_user
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'content_moderation'
)\gexec
