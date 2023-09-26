update votes set state = json_set(state, '$.data.context.guild', 0);
