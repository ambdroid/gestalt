BEGIN TRANSACTION ;
create table deleted(id text unique collate nocase);
insert into deleted select maskid as mskid from history where typeof(mskid) = 'text' and length(mskid) = 5 and not exists(select 1 from masks where maskid = mskid);
COMMIT TRANSACTION ;

