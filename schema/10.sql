BEGIN TRANSACTION ;
update users set prefs = prefs & ~(1 << 5);
update proxies set flags = flags & ~(1 << 0);
create table if not exists proxiesnew(proxid text primary key collate nocase,cmdname text collate nocase,userid integer,guildid integer,prefix text,postfix text,type integer,otherid integer,maskid text collate nocase,flags integer,state integer,unique(userid, maskid));
insert into proxiesnew select proxid,cmdname,userid,guildid,prefix,postfix,type,otherid,maskid,flags,state from proxies;
drop table proxies;
alter table proxiesnew rename to proxies;
COMMIT TRANSACTION ;
