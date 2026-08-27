BEGIN TRANSACTION ;
alter table users drop column username;
delete from proxies where not exists (select 1 from users where users.userid = proxies.userid);
COMMIT TRANSACTION ;
