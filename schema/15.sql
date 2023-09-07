BEGIN TRANSACTION ;
create table if not exists proxiesnew(proxid text primary key collate nocase,cmdname text collate nocase,userid integer,guildid integer,prefix text,postfix text,type integer,otherid integer,maskid text collate nocase,flags integer,state integer,created integer,msgcount integer,unique(maskid, userid));
insert into proxiesnew select * from proxies;
drop table proxies;
alter table proxiesnew rename to proxies;
COMMIT TRANSACTION ;
