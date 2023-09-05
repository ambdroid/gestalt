BEGIN TRANSACTION ;
create table historynew(msgid integer primary key,origid integer,threadid integer,chanid integer,guildid integer,authid integer,otherid integer,proxid text,maskid text);
insert into historynew select msgid,NULL,threadid,chanid,NULL,authid,otherid,proxid,maskid from history;
drop table history;
alter table historynew rename to history;
alter table proxies add column created integer;
alter table proxies add column msgcount integer;
update proxies set (created, msgcount) = (NULL, NULL);
create table masksnew(maskid text collate nocase,guildid integer,roleid integer,nick text,avatar text,color text,type integer,created integer,updated integer,msgcount integer,unique(maskid, guildid),unique(guildid, roleid));
insert into masksnew select maskid,guildid,roleid,nick,avatar,color,type,NULL,updated,NULL from masks;
drop table masks;
alter table masksnew rename to masks;
COMMIT TRANSACTION ;