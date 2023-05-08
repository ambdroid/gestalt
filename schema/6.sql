BEGIN TRANSACTION ;
create table if not exists webhooksnew(chanid integer primary key,hookid integer unique,token text);
insert into webhooksnew select chanid, hookid, token from webhooks;
drop table webhooks;
alter table webhooksnew rename to webhooks;
COMMIT TRANSACTION ;

