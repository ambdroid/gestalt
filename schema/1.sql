BEGIN TRANSACTION;
create table proxiesnew(proxid text primary key collate nocase,cmdname text collate nocase,userid integer,guildid integer,prefix text,postfix text,type integer,otherid integer,maskid text,auto integer,become real,state integer,unique(userid, otherid),unique(userid, maskid));
insert into proxiesnew select proxid,"",userid,guildid,prefix,postfix,type,otherid,maskid,auto,become,state from proxies;
drop table proxies;
alter table proxiesnew rename to proxies;
COMMIT TRANSACTION;
