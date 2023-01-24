BEGIN TRANSACTION ;
create table proxiesnew(proxid text primary key collate nocase,cmdname text collate nocase,userid integer,guildid integer,prefix text,postfix text,type integer,otherid integer,maskid text,flags integer,become real,state integer,unique(userid, maskid));
insert into proxiesnew select * from proxies;
drop table proxies;
alter table proxiesnew rename to proxies;
create table masksnew(maskid text collate nocase,guildid integer,roleid integer,nick text,avatar text,type int,updated int,unique(maskid, guildid),unique(guildid, roleid));
insert into masksnew select maskid,guildid,roleid,nick,avatar,1,NULL from masks;
drop table masks;
alter table masksnew rename to masks;
COMMIT TRANSACTION ;