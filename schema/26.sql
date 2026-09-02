BEGIN TRANSACTION ;
delete from users where (not exists (select 1 from proxies where users.userid == proxies.userid and (proxies.type != 0 or proxies.created > unixepoch('2025-06-08 20:40:33-04:00')))) and (not exists (select 1 from history where history.authid == users.userid and chanid != 0));
delete from members where not exists (select 1 from users where users.userid = members.userid);
delete from taken where exists(select 1 from proxies where (proxies.proxid, type) == (taken.id, 0) and not exists (select 1 from users where users.userid = proxies.userid));
delete from proxies where type == 0 and not exists (select 1 from users where users.userid = proxies.userid);
COMMIT TRANSACTION ;
