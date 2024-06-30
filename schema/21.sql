update proxies set msgcount = (select count() from history where history.proxid = proxies.proxid);
