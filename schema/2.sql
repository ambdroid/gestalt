BEGIN TRANSACTION;
drop trigger auto_exclusive;
alter table proxies rename column auto to flags;
COMMIT TRANSACTION;
