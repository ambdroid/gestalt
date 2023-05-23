# delete inactive swaps
delete from proxies where (type, state) = (2, 0);
