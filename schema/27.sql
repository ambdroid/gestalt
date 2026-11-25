BEGIN TRANSACTION ;
drop trigger mask_delete;
delete from guildmasks where type = 3 and not exists (select 1 from proxies where proxies.maskid = guildmasks.maskid);
COMMIT TRANSACTION ;
