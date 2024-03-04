BEGIN TRANSACTION ;
update proxies set (prefix, postfix) = (NULL, NULL) where type = 4;
insert or ignore into deleted select proxid from proxies;
insert or ignore into deleted select maskid from masks;
alter table deleted rename to taken;
COMMIT TRANSACTION ;
