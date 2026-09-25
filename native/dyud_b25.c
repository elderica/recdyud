/*
 * dyud_b25.c - thin C shim between recdyud (Python/ctypes) and libaribb25.
 *
 * libaribb25 talks to the B-CAS card through the B_CAS_CARD vtable, whose
 * stock implementation (b_cas_card.c) requires PC/SC.  The DY-UD200 has the
 * card slot inside the tuner and the card is reached through vendor USB
 * commands, so recdyud implements the card on the Python side and this shim
 * forwards ECM/EMM requests to Python callbacks.
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdlib.h>
#include <string.h>

#include "arib_std_b25.h"
#include "arib_std_b25_error_code.h"
#include "b_cas_card.h"
#include "b_cas_card_error_code.h"

#if defined(_WIN32)
#define RECDYUD_API __declspec(dllexport)
#else
#define RECDYUD_API __attribute__((visibility("default")))
#endif

#define RECDYUD_MAX_CARD_IDS 16

/* Returns 0 on success, negative on failure. */
typedef int (*recdyud_ecm_fn)(void *user, const uint8_t *ecm, int32_t len,
                              uint8_t *scramble_key, uint32_t *return_code);
typedef int (*recdyud_emm_fn)(void *user, const uint8_t *emm, int32_t len);

typedef struct {
	B_CAS_CARD card; /* must be the first member */
	B_CAS_INIT_STATUS status;
	int64_t ids[RECDYUD_MAX_CARD_IDS];
	int32_t id_count;
	recdyud_ecm_fn ecm;
	recdyud_emm_fn emm;
	void *user;
} recdyud_card;

typedef struct {
	ARIB_STD_B25 *b25;
	recdyud_card card;
	int has_card;
} recdyud_b25;

static recdyud_card *card_of(void *bcas)
{
	B_CAS_CARD *c = (B_CAS_CARD *)bcas;
	return c ? (recdyud_card *)c->private_data : NULL;
}

static void card_release(void *bcas)
{
	/* owned by recdyud_b25 */
	(void)bcas;
}

static int card_init(void *bcas)
{
	return card_of(bcas) ? 0 : B_CAS_CARD_ERROR_INVALID_PARAMETER;
}

static int card_get_init_status(void *bcas, B_CAS_INIT_STATUS *stat)
{
	recdyud_card *c = card_of(bcas);
	if (c == NULL || stat == NULL) {
		return B_CAS_CARD_ERROR_INVALID_PARAMETER;
	}
	memcpy(stat, &c->status, sizeof(*stat));
	return 0;
}

static int card_get_id(void *bcas, B_CAS_ID *dst)
{
	recdyud_card *c = card_of(bcas);
	if (c == NULL || dst == NULL) {
		return B_CAS_CARD_ERROR_INVALID_PARAMETER;
	}
	dst->data = c->ids;
	dst->count = c->id_count;
	return 0;
}

static int card_get_pwr_on_ctrl(void *bcas, B_CAS_PWR_ON_CTRL_INFO *dst)
{
	recdyud_card *c = card_of(bcas);
	if (c == NULL || dst == NULL) {
		return B_CAS_CARD_ERROR_INVALID_PARAMETER;
	}
	dst->data = NULL;
	dst->count = 0;
	return 0;
}

static int card_proc_ecm(void *bcas, B_CAS_ECM_RESULT *dst, uint8_t *src, int len)
{
	recdyud_card *c = card_of(bcas);
	if (c == NULL || dst == NULL || src == NULL || len < 1) {
		return B_CAS_CARD_ERROR_INVALID_PARAMETER;
	}
	if (c->ecm == NULL) {
		return B_CAS_CARD_ERROR_NOT_INITIALIZED;
	}
	memset(dst, 0, sizeof(*dst));
	if (c->ecm(c->user, src, len, dst->scramble_key, &dst->return_code) < 0) {
		return B_CAS_CARD_ERROR_TRANSMIT_FAILED;
	}
	return 0;
}

static int card_proc_emm(void *bcas, uint8_t *src, int len)
{
	recdyud_card *c = card_of(bcas);
	if (c == NULL || src == NULL || len < 1) {
		return B_CAS_CARD_ERROR_INVALID_PARAMETER;
	}
	if (c->emm == NULL) {
		return B_CAS_CARD_ERROR_NOT_INITIALIZED;
	}
	if (c->emm(c->user, src, len) < 0) {
		return B_CAS_CARD_ERROR_TRANSMIT_FAILED;
	}
	return 0;
}

static int card_set_acas_mode(void *bcas, int enable)
{
	(void)bcas;
	/* The DY-UD200 only handles B-CAS cards. */
	return enable ? B_CAS_CARD_ERROR_INVALID_PARAMETER : 0;
}

RECDYUD_API recdyud_b25 *recdyud_b25_create(int32_t round, int32_t strip, int32_t emm_proc)
{
	recdyud_b25 *h = (recdyud_b25 *)calloc(1, sizeof(recdyud_b25));
	if (h == NULL) {
		return NULL;
	}
	h->b25 = create_arib_std_b25();
	if (h->b25 == NULL) {
		free(h);
		return NULL;
	}
	if (h->b25->set_multi2_round(h->b25, round) < 0 ||
	    h->b25->set_strip(h->b25, strip) < 0 ||
	    h->b25->set_emm_proc(h->b25, emm_proc) < 0) {
		h->b25->release(h->b25);
		free(h);
		return NULL;
	}
	return h;
}

RECDYUD_API int recdyud_b25_set_card(recdyud_b25 *h,
                                     const uint8_t *system_key, /* 32 bytes */
                                     const uint8_t *init_cbc,   /* 8 bytes */
                                     int64_t card_id,
                                     int32_t card_status,
                                     int32_t ca_system_id,
                                     const int64_t *ids, int32_t id_count,
                                     recdyud_ecm_fn ecm, recdyud_emm_fn emm, void *user)
{
	recdyud_card *c;

	if (h == NULL || system_key == NULL || init_cbc == NULL || ecm == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	if (id_count < 0 || id_count > RECDYUD_MAX_CARD_IDS || (id_count > 0 && ids == NULL)) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}

	c = &h->card;
	memset(c, 0, sizeof(*c));
	c->card.private_data = c;
	c->card.release = card_release;
	c->card.init = card_init;
	c->card.get_init_status = card_get_init_status;
	c->card.get_id = card_get_id;
	c->card.get_pwr_on_ctrl = card_get_pwr_on_ctrl;
	c->card.proc_ecm = card_proc_ecm;
	c->card.proc_emm = card_proc_emm;
	c->card.set_acas_mode = card_set_acas_mode;

	memcpy(c->status.system_key, system_key, sizeof(c->status.system_key));
	memcpy(c->status.init_cbc, init_cbc, sizeof(c->status.init_cbc));
	c->status.bcas_card_id = card_id;
	c->status.card_status = card_status;
	c->status.ca_system_id = ca_system_id;
	if (id_count > 0) {
		memcpy(c->ids, ids, sizeof(int64_t) * (size_t)id_count);
	}
	c->id_count = id_count;
	c->ecm = ecm;
	c->emm = emm;
	c->user = user;
	h->has_card = 1;

	return h->b25->set_b_cas_card(h->b25, &c->card);
}

RECDYUD_API int recdyud_b25_put(recdyud_b25 *h, const uint8_t *data, uint32_t size)
{
	ARIB_STD_B25_BUFFER buf;
	if (h == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	buf.data = (uint8_t *)data; /* put() copies the data into its own buffer */
	buf.size = size;
	return h->b25->put(h->b25, &buf);
}

RECDYUD_API int recdyud_b25_get(recdyud_b25 *h, const uint8_t **data, uint32_t *size)
{
	ARIB_STD_B25_BUFFER buf;
	int r;
	if (h == NULL || data == NULL || size == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	buf.data = NULL;
	buf.size = 0;
	r = h->b25->get(h->b25, &buf);
	*data = buf.data;
	*size = buf.size;
	return r;
}

RECDYUD_API int recdyud_b25_withdraw(recdyud_b25 *h, const uint8_t **data, uint32_t *size)
{
	ARIB_STD_B25_BUFFER buf;
	int r;
	if (h == NULL || data == NULL || size == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	buf.data = NULL;
	buf.size = 0;
	r = h->b25->withdraw(h->b25, &buf);
	*data = buf.data;
	*size = buf.size;
	return r;
}

RECDYUD_API int recdyud_b25_flush(recdyud_b25 *h)
{
	if (h == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	return h->b25->flush(h->b25);
}

RECDYUD_API int recdyud_b25_reset(recdyud_b25 *h)
{
	if (h == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	return h->b25->reset(h->b25);
}

RECDYUD_API int recdyud_b25_program_count(recdyud_b25 *h)
{
	if (h == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	return h->b25->get_program_count(h->b25);
}

RECDYUD_API int recdyud_b25_program_info(recdyud_b25 *h, int32_t idx,
                                         int32_t *program_number,
                                         int32_t *ecm_unpurchased_count,
                                         int32_t *last_ecm_error_code,
                                         int64_t *total_packet_count,
                                         int64_t *undecrypted_packet_count)
{
	ARIB_STD_B25_PROGRAM_INFO info;
	int r;
	if (h == NULL) {
		return ARIB_STD_B25_ERROR_INVALID_PARAM;
	}
	memset(&info, 0, sizeof(info));
	r = h->b25->get_program_info(h->b25, &info, idx);
	if (r < 0) {
		return r;
	}
	if (program_number) *program_number = info.program_number;
	if (ecm_unpurchased_count) *ecm_unpurchased_count = info.ecm_unpurchased_count;
	if (last_ecm_error_code) *last_ecm_error_code = info.last_ecm_error_code;
	if (total_packet_count) *total_packet_count = info.total_packet_count;
	if (undecrypted_packet_count) *undecrypted_packet_count = info.undecrypted_packet_count;
	return 0;
}

RECDYUD_API void recdyud_b25_destroy(recdyud_b25 *h)
{
	if (h == NULL) {
		return;
	}
	if (h->b25 != NULL) {
		h->b25->release(h->b25);
	}
	free(h);
}
