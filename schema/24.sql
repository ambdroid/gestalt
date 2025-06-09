BEGIN TRANSACTION ;
delete from users where (select count() from proxies where proxies.userid == users.userid) == 1 and not exists (select 1 from history where history.authid = users.userid);
delete from members where not exists (select 1 from users where users.userid = members.userid);
delete from taken where exists(select 1 from proxies where proxies.proxid == taken.id and not exists (select 1 from users where users.userid = proxies.userid));
delete from proxies where not exists (select 1 from users where users.userid = proxies.userid);
COMMIT TRANSACTION ;
