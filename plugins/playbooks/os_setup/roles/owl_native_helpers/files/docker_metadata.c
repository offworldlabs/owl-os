/* Read-only Docker metadata fast path. Nonzero means caller may use the CLI. */
#define _GNU_SOURCE 1
#include <curl/curl.h>
#include <json-c/json.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define BODY_LIMIT 65536U
#define CONFIG_LIMIT 65536U
#ifndef OWL_DOCKER_SOCKET
#define OWL_DOCKER_SOCKET "/var/run/docker.sock"
#endif
#ifndef OWL_DOCKER_CONTAINER
#define OWL_DOCKER_CONTAINER "blah2"
#endif

struct body {
    char            bytes[BODY_LIMIT + 1];
    size_t          length;
};

static size_t receive_body(void *data, size_t size, size_t count, void *opaque){
    struct body    *body = opaque;
    size_t          length;
    if (count != 0 && size > SIZE_MAX / count)
        return 0;
    length = size * count;
    if (length > BODY_LIMIT - body->length)
        return 0;
    memcpy(body->bytes + body->length, data, length);
    body->length += length;
    return length;
}

static int
has_docker_override(void)
{
    static const char *const names[] = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_API_VERSION", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", NULL};
    size_t          i;
    for (i = 0; names[i] != NULL; ++i) {
        const char     *value = getenv(names[i]);
        if (value != NULL && value[0] != '\0')
            return 1;
    }
    return 0;
}

/* JSON-C accepts trailing JSON whitespace, but no other bytes after a value. */
static struct json_object *
parse_json_complete(const char *bytes, size_t length)
{
    struct json_tokener *tokener;
    struct json_object *object;
    size_t          end, i;
    if (length == 0 || memchr(bytes, '\0', length) != NULL || length > INT_MAX)
        return NULL;
    tokener = json_tokener_new_ex(64);
    if (tokener == NULL)
        return NULL;
    json_tokener_set_flags(tokener, JSON_TOKENER_STRICT);
    object = json_tokener_parse_ex(tokener, bytes, (int)length);
    if (json_tokener_get_error(tokener) != json_tokener_success) {
        if (object != NULL)
            json_object_put(object);
        object = NULL;
        goto out;
    }
    end = (size_t) json_tokener_get_parse_end(tokener);
    for (i = end; i < length; ++i)
        if (bytes[i] != ' ' && bytes[i] != '\t' && bytes[i] != '\r' && bytes[i] != '\n') {
            json_object_put(object);
            object = NULL;
            break;
        }
out:
    json_tokener_free(tokener);
    return object;
}

static int
config_is_safe(void)
{
    const char     *home = getenv("HOME");
    char            path[PATH_MAX], buffer[CONFIG_LIMIT];
    struct stat     status;
    struct json_object *root = NULL, *context = NULL;
    int             fd, result = 0;
    size_t          used = 0;
    if (home == NULL || home[0] == '\0')
        return 0; /* Docker may resolve a passwd home; defer to its CLI. */
    if (snprintf(path, sizeof(path), "%s/.docker/config.json", home) >= (int)sizeof(path))
        return 0;
    fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0)
        return errno == ENOENT;
    if (fstat(fd, &status) != 0 || !S_ISREG(status.st_mode) || status.st_size < 0 || (uintmax_t) status.st_size > CONFIG_LIMIT)
        goto out;
    while (used < sizeof(buffer)) {
        ssize_t         n = read(fd, buffer + used, sizeof(buffer) - used);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            goto out;
        }
        if (n == 0)
            break;
        used += (size_t) n;
    }
    if (used == sizeof(buffer)) {
        char            extra;
        if (read(fd, &extra, 1) != 0)
            goto out;
    }
    root = parse_json_complete(buffer, used);
    if (root == NULL || !json_object_is_type(root, json_type_object))
        goto out;
    if (!json_object_object_get_ex(root, "currentContext", &context))
        result = 1;
    else if (json_object_is_type(context, json_type_string) &&
             json_object_get_string_len(context) == (int)strlen("default") &&
             memcmp(json_object_get_string(context), "default", strlen("default")) == 0)
        result = 1;
out:
    if (root != NULL)
        json_object_put(root);
    close(fd);
    return result;
}

static int
valid_container_name(const char *name)
{
    size_t          i;
    if (name[0] == '\0')
        return 0;
    for (i = 0; name[i] != '\0'; ++i) {
        unsigned char   c = (unsigned char)name[i];
        if (!(isalnum(c) || c == '_' || c == '.' || c == '-'))
            return 0;
    }
    return 1;
}
static int
leap_year(int year)
{
    return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
}
static int
decimal(const char *text, size_t start, size_t count)
{
    int             value = 0;
    size_t          i;
    for (i = start; i < start + count; ++i) {
        if (text[i] < '0' || text[i] > '9')
            return -1;
        value = value * 10 + text[i] - '0';
    }
    return value;
}
static int
valid_started_at(const char *text, size_t length)
{
    static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    int             year, month, day, hour, minute, second, max_day, zone_hour, zone_minute;
    size_t          i;
    if (length < 20)
        return 0;
    for (i = 0; i < length; ++i)
        if ((unsigned char)text[i] <= 0x20 || (unsigned char)text[i] == 0x7f)
            return 0;
    if (text[4] != '-' || text[7] != '-' || text[10] != 'T' || text[13] != ':' || text[16] != ':')
        return 0;
    year = decimal(text, 0, 4);
    month = decimal(text, 5, 2);
    day = decimal(text, 8, 2);
    hour = decimal(text, 11, 2);
    minute = decimal(text, 14, 2);
    second = decimal(text, 17, 2);
    if (year < 0 || month < 1 || month > 12 || day < 1 || hour < 0 || hour > 23 ||
        minute < 0 || minute > 59 || second < 0 || second > 60)
        return 0;
    max_day = days[month - 1] + (month == 2 && leap_year(year));
    if (day > max_day)
        return 0;
    i = 19;
    if (i < length && text[i] == '.') {
        size_t          first = ++i;
        while (i < length && text[i] >= '0' && text[i] <= '9')
            ++i;
        if (i == first)
            return 0;
    }
    if (i == length - 1 && text[i] == 'Z')
        return 1;
    if (i + 6 != length || (text[i] != '+' && text[i] != '-') || text[i + 3] != ':')
        return 0;
    zone_hour = decimal(text, i + 1, 2);
    zone_minute = decimal(text, i + 4, 2);
    return zone_hour >= 0 && zone_hour <= 23 && zone_minute >= 0 && zone_minute <= 59;
}

static int
extract_metadata(const struct body *body, long code)
{
    struct json_object *root = NULL, *restart = NULL, *state = NULL, *started = NULL, *message = NULL;
    int             result = 1;
    root = parse_json_complete(body->bytes, body->length);
    if (root == NULL || !json_object_is_type(root, json_type_object))
        goto out;
    if (code == 404) {
        static const char absent[] = "No such container: " OWL_DOCKER_CONTAINER;
        if (json_object_object_get_ex(root, "message", &message) &&
            json_object_is_type(message, json_type_string) &&
            json_object_get_string_len(message) == (int)(sizeof(absent) - 1) &&
            memcmp(json_object_get_string(message), absent, sizeof(absent) - 1) == 0)
            result = 2;
        goto out;
    }
    if (code != 200 || !json_object_object_get_ex(root, "RestartCount", &restart) ||
        !json_object_is_type(restart, json_type_int) || json_object_get_int64(restart) < 0 ||
        json_object_get_uint64(restart) > INT64_MAX ||
        !json_object_object_get_ex(root, "State", &state) ||
        !json_object_is_type(state, json_type_object) ||
        !json_object_object_get_ex(state, "StartedAt", &started) ||
        !json_object_is_type(started, json_type_string))
        goto out;
    {
        const char     *timestamp = json_object_get_string(started);
        size_t          timestamp_length = (size_t) json_object_get_string_len(started);
        int64_t         count = json_object_get_int64(restart);
        if (timestamp == NULL || !valid_started_at(timestamp, timestamp_length))
            goto out;
        printf("%lld %.*s\n", (long long)count, (int)timestamp_length, timestamp);
    }
    result = 0;
out:
    if (root != NULL)
        json_object_put(root);
    return result;
}

int
main(void)
{
    CURL           *curl;
    CURLcode        curl_result;
    struct body     body = {{0}, 0};
    long            status = 0;
    char            url[256];
    if (has_docker_override() || !config_is_safe() || !valid_container_name(OWL_DOCKER_CONTAINER) || snprintf(url, sizeof(url), "http://localhost/v1.44/containers/%s/json", OWL_DOCKER_CONTAINER) >= (int)sizeof(url))
        return 1;
    curl = curl_easy_init();
    if (curl == NULL)
        return 1;
#define CURL_OK(option, value) do { if (curl_easy_setopt(curl, option, value) != CURLE_OK) goto curl_out; } while (0)
    CURL_OK(CURLOPT_UNIX_SOCKET_PATH, OWL_DOCKER_SOCKET);
    CURL_OK(CURLOPT_URL, url);
    CURL_OK(CURLOPT_HTTPGET, 1L);
    CURL_OK(CURLOPT_FOLLOWLOCATION, 0L);
    CURL_OK(CURLOPT_PROXY, "");
    CURL_OK(CURLOPT_NOPROXY, "*");
    CURL_OK(CURLOPT_WRITEFUNCTION, receive_body);
    CURL_OK(CURLOPT_WRITEDATA, &body);
    CURL_OK(CURLOPT_CONNECTTIMEOUT_MS, 100L);
    CURL_OK(CURLOPT_TIMEOUT_MS, 500L);
    CURL_OK(CURLOPT_NOSIGNAL, 1L);
    curl_result = curl_easy_perform(curl);
    if (curl_result == CURLE_OK && curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status) == CURLE_OK) {
        curl_easy_cleanup(curl);
        return extract_metadata(&body, status);
    }
curl_out:
    curl_easy_cleanup(curl);
    return 1;
#undef CURL_OK
}
