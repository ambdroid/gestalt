delete from guildmasks where type = 5 and exists(select 1 from deleted where maskid = id) and not exists(select 1 from masks where masks.maskid = guildmasks.maskid);
