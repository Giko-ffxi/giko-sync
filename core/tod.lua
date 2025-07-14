local timer   = require('lib.ashita.timer')
local cache   = require('lib.giko.cache')
local config  = require('lib.giko.config')
local common  = require('lib.giko.common')
local monster = require('lib.giko.monster')
local death   = require('lib.giko.death')
local chat    = require('lib.giko.chat')
local json    = require('json.json')
local http    = require('socket.http')
local ltn12   = require("ltn12")
local socket  = require('socket')

local tod     = {}

local function create_timeout_socket()
    local sock = socket.tcp()
    sock:settimeout(1, 'b')
    sock:settimeout(3, 't')
    return sock
end

tod.get = function()
    local resp = ''
    local c_tods = {}
    local s_tods = {}
    local u_flag = false
    local tell = {}

    local request_function = function()
        return http.request({
            method = "GET",
            url = config.tod.get,
            create = create_timeout_socket,
            sink = function(chunk)
                if chunk ~= nil then
                    resp = resp .. chunk
                end
                return true
            end
        })
    end

    local pcall_success, ok_or_error, code_or_nil = pcall(request_function)

    if not pcall_success then
        print(string.format("[Giko/TOD GET] Request failed or timed out: %s", tostring(ok_or_error)))
        return s_tods
    end

    local ok = ok_or_error
    local code = code_or_nil

    if ok and code == 200 and resp ~= '' then
        for r in string.gmatch(resp, "[^\r\n]+") do
            local name, data = string.match(r, "(.+\)|(\{[^}]*\})")
            if name and data then
                local mob = monster.get(name)
                if mob then
                    local s_tod = json:decode(data)
                    if s_tod then
                        local c_tod_json = cache.get(death.cache, mob.names.nq[1])
                        local c_tod = (c_tod_json and json:decode(c_tod_json)) or nil
                        if c_tod == nil or (c_tod.created_at ~= nil and s_tod.created_at ~= nil and c_tod.created_at < s_tod.created_at) then
                            local expected_server_day = c_tod.day > 0 and c_tod.day + 1 or c_tod.day
                            if c_tod == nil or s_tod.gmt ~= c_tod.gmt or s_tod.day ~= expected_server_day then
                                table.insert(tell,
                                    string.format("@giko set-tod %s %s %s --force", mob.names.nq[1],
                                        common.gmt_to_local_date(s_tod.gmt), s_tod.day or 0))
                            end
                            c_tods[string.lower(mob.names.nq[1])] = json:encode(s_tod)
                            u_flag = true
                        end
                        s_tods[string.lower(mob.names.nq[1])] = s_tod
                    end
                end
            end
        end

        if u_flag then
            for k, v in ipairs(tell) do
                ashita.timer.create(string.format('giko-sync-%s', k), (k * 2), 1,
                    function() chat.tell(config.broadcaster, v) end)
            end
        end
    elseif ok and code ~= 200 then
        print(string.format("[Giko/TOD GET] HTTP request returned status: %s", tostring(code)))
    elseif not ok then
        print(string.format("[Giko/TOD GET] HTTP request 'ok' was false/nil. Error from http.request: %s", tostring(code)))
    end

    return s_tods
end


tod.set = function(s_tods_from_get)
    local resp           = ''
    local lines          = cache.get_all(death.cache)
    local c_tods_to_send = {}

    for name, tod_json in pairs(lines) do
        local mob = monster.get(name)
        if mob then
            local lower_nq_name = string.lower(mob.names.nq[1])
            if common.in_array_key(s_tods_from_get, lower_nq_name) then
                local c_tod = json:decode(tod_json)
                local s_tod = s_tods_from_get[lower_nq_name]
                if c_tod and s_tod and c_tod.created_at and s_tod.created_at and
                    c_tod.created_at > s_tod.created_at and
                    (c_tod.gmt ~= s_tod.gmt or c_tod.day ~= s_tod.day) then
                    c_tods_to_send[lower_nq_name] = json:encode(c_tod)
                end
            else
                local c_tod = json:decode(tod_json)
                c_tods_to_send[lower_nq_name] = json:encode(c_tod)
            end
        end
    end

    if common.size(c_tods_to_send) > 0 then
        local post_data = json:encode(c_tods_to_send)

        local request_function = function()
            local resp_body_chunks          = {}
            local ok, code, headers, status = http.request({
                method = "POST",
                url = config.tod.set,
                source = ltn12.source.string(post_data),
                headers = {
                    ["Content-Type"] = "application/json; charset=utf-8",
                    ["Content-Length"] = #post_data
                },
                create = create_timeout_socket,
                sink = ltn12.sink.table(resp_body_chunks)
            })
            local full_resp_body            = table.concat(resp_body_chunks)
            return full_resp_body, code, ok, headers, status
        end

        local pcall_success, returned_body, returned_code, req_ok, req_headers, req_status = pcall(request_function)


        if not pcall_success then
            print(string.format("[Giko/TOD SET] Request failed or timed out: %s", tostring(returned_body)))
        else
            if returned_body and returned_code == 202 then
                -- print(string.format("[Giko/TOD SET] Successfully sent. Response: %s", resp))
            elseif returned_body then
                --print(string.format("[Giko/TOD SET] Sent, but HTTP status was: %s. Response: %s", tostring(returned_code),
                --returned_body))
            else
                print(string.format("[Giko/TOD SET] HTTP request was false/nil. Error from http.request: %s",
                    tostring(returned_code)))
            end
        end
    end

    return c_tods_to_send
end

return tod
