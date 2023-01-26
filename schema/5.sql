BEGIN TRANSACTION ;
create table historynew(msgid integer primary key,threadid integer,chanid integer,authid integer,otherid integer,proxid text,maskid text);
insert into historynew select msgid,0,chanid,authid,otherid,NULL,maskid from history;
drop table history;
alter table historynew rename to history;
COMMIT TRANSACTION ;
