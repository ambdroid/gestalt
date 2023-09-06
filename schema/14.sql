BEGIN TRANSACTION ;
update masks set rules = json_object('type', json_extract(rules, '$._type'), 'data', json_remove(rules, '$._type'));
update votes set state = json_object('type', json_extract(state, '$._type'), 'data', json_remove(state, '$._type'));
COMMIT TRANSACTION ;
