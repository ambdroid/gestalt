BEGIN TRANSACTION ;
insert into deleted select proxid from proxies where type = 1 and not exists(select 1 from masks where masks.maskid = proxies.maskid);
delete from proxies where type = 1 and not exists(select 1 from masks where masks.maskid = proxies.maskid);
update proxies set otherid = (select roleid from masks where masks.maskid = proxies.maskid) where type = 1;
COMMIT TRANSACTION ;
