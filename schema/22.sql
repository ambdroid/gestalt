BEGIN TRANSACTION ;
create table historynew(msgid integer primary key,origid integer,chanid integer,parentid integer,guildid integer,authid integer,otherid integer,proxid text,maskid text);
insert into historynew select msgid, origid, case threadid when 0 then chanid else threadid end, case threadid when 0 then 0 else chanid end, guildid, authid, otherid, proxid, maskid from history;
drop table history;
alter table historynew rename to history;
COMMIT TRANSACTION ;
