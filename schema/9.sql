BEGIN TRANSACTION ;
create table if not exists masksnew(maskid text collate nocase,guildid integer,roleid integer,nick text,avatar text,color text,type int,updated int,unique(maskid, guildid),unique(guildid, roleid));
insert into masksnew select maskid,guildid,roleid,nick,avatar,NULL,type,updated from masks;
drop table masks;
alter table masksnew rename to masks;
alter table users add column tag text;
alter table users add column color text;
update users set tag = '';
COMMIT TRANSACTION ;
