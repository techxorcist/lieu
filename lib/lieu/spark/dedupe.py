from six import itertools

from lieu.address import AddressComponents, EntityDetails, Coordinates
from lieu.api import DedupeResponse
from lieu.dedupe import AddressDeduper, NameAddressDeduper, PhoneNumberDeduper
from lieu.word_index import WordIndex

from lieu.spark.geo_word_index import GeoWordIndexSpark
from lieu.spark.information_gain import InformationGainSpark, GeoInformationGainSpark
from lieu.spark.tfidf import TFIDFSpark, GeoTFIDFSpark
from lieu.spark.utils import IDPairRDD

from postal.dedupe import duplicate_status


class AddressDeduperSpark(object):
    @classmethod
    def address_dupe_pairs(cls, address_hashes, addresses_and_languages, sub_building=False, fuzzy_street_name=False):
        candidate_dupe_pairs = address_hashes.groupByKey() \
                                             .filter(lambda (key, vals): len(vals) > 1) \
                                             .values() \
                                             .flatMap(lambda vals: [(max(uid1, uid2), min(uid1, uid2)) for uid1, uid2 in itertools.combinations(vals, 2)]) \
                                             .distinct()

        dupe_pairs = IDPairRDD.join_pairs(candidate_dupe_pairs, addresses_and_languages) \
                              .mapValues(lambda ((a1, langs1), (a2, langs2)): (a1, a2, AddressDeduper.combined_languages(langs1, langs2))) \
                              .mapValues(lambda (a1, a2, all_langs): (AddressDeduper.address_dupe_status(a1, a2, languages=all_langs, fuzzy_street_name=fuzzy_street_name), (not sub_building or AddressDeduper.is_sub_building_dupe(a1, a2, languages=all_langs)))) \
                              .filter(lambda ((uid1, uid2), (address_dupe_status, is_sub_building_dupe)): address_dupe_status in (duplicate_status.EXACT_DUPLICATE, duplicate_status.LIKELY_DUPLICATE) and is_sub_building_dupe) \
                              .map(lambda ((uid1, uid2), (address_dupe_status, is_sub_building_dupe)): ((uid1, uid2), address_dupe_status))

        return dupe_pairs

    @classmethod
    def address_languages(cls, id_address):
        return id_address.mapValues(lambda address: AddressDeduper.address_languages(address))

    @classmethod
    def dupe_sims(cls, address_ids, with_unit=False, with_city_or_equivalent=False, with_small_containing_boundaries=False, with_postal_code=False, with_latlon=True, fuzzy_street=False):
        id_address = address_ids.map(lambda (address, uid): (uid, address))
        address_languages = cls.address_languages(id_address)
        addresses_and_languages = id_address.join(address_languages)

        address_hashes = addresses_and_languages.flatMap(lambda (uid, (address, langs)): [(h, uid) for h in AddressDeduper.near_dupe_hashes(address, languages=langs, with_address=with_address, with_unit=with_unit, with_city_or_equivalent=with_city_or_equivalent, \
                                                                                                                                            with_small_containing_boundaries=with_small_containing_boundaries, with_postal_code=with_postal_code, with_latlon=with_latlon)])

        return cls.address_dupe_pairs(address_hashes, addresses_and_languages) \
                  .map(lambda ((uid1, uid2), dupe_status): ((uid1, uid2), (dupe_status, 1.0)))


class NameAddressDeduperSpark(object):
    DEFAULT_GEO_MODEL_PROPORTION = 0.6

    @classmethod
    def names(cls, address_ids):
        return address_ids.map(lambda (address, uid): (address.get(AddressComponents.NAME, ''), uid))

    @classmethod
    def names_geo(cls, address_ids):
        return address_ids.map(lambda (address, uid): ((address.get(AddressComponents.NAME, ''), address.get(Coordinates.LATITUDE), address.get(Coordinates.LONGITUDE)), uid) )

    @classmethod
    def batch_doc_count(cls, docs):
        return docs.count()

    @classmethod
    def name_similarity(cls, tokens1, scores1, tokens2, scores2, languages=None, name_dupe_threshold=DedupeResponse.default_name_dupe_threshold,
                        name_review_threshold=DedupeResponse.default_name_review_threshold):
        scores_norm1 = WordIndex.normalized_vector(scores1)

        scores_norm2 = WordIndex.normalized_vector(scores2)

        return NameAddressDeduper.name_dupe_fuzzy(tokens1, scores_norm1, tokens2, scores_norm2, languages=languages,
                                                  likely_dupe_threshold=name_dupe_threshold, needs_review_threshold=name_review_threshold)

    @classmethod
    def name_geo_dupe_similarity(cls, tokens1, scores1, geo_scores1, tokens2, scores2, geo_scores2, languages=None, geo_model_proportion=DEFAULT_GEO_MODEL_PROPORTION,
                                 name_dupe_threshold=DedupeResponse.default_name_dupe_threshold, name_review_threshold=DedupeResponse.default_name_review_threshold):
        dupe_class, sim = cls.name_similarity(tokens1, scores1, tokens2, scores2, languages=languages,
                                              name_dupe_threshold=name_dupe_threshold, name_review_threshold=name_review_threshold)

        geo_dupe_class, geo_sim = cls.name_similarity(tokens1, geo_scores1, tokens2, geo_scores2, languages=languages,
                                                      name_dupe_threshold=name_dupe_threshold, name_review_threshold=name_review_threshold)

        combined_sim = (geo_model_proportion * geo_sim) + ((1.0 - geo_model_proportion) * sim)
        dupe_class = duplicate_status.LIKELY_DUPLICATE if combined_sim >= name_dupe_threshold else duplicate_status.NEEDS_REVIEW if combined_sim >= name_review_threshold else duplicate_status.NON_DUPLICATE
        return dupe_class, combined_sim

    @classmethod
    def names_tfidf(cls, name_ids, total_docs, doc_frequency=None, min_count=1, index_path=None):
        name_words = TFIDFSpark.doc_words(name_ids, has_id=True)
        name_word_counts = TFIDFSpark.doc_word_counts(name_ids, has_id=True)
        batch_doc_frequency = TFIDFSpark.doc_frequency(name_word_counts)

        if doc_frequency is not None:
            doc_frequency = TFIDFSpark.update_doc_frequency(doc_frequency, batch_doc_frequency)
        else:
            doc_frequency = batch_doc_frequency

        names_tfidf = TFIDFSpark.doc_scores(name_words, doc_frequency, total_docs)

        return names_tfidf.mapValues(lambda values: zip(*values))

    @classmethod
    def names_information_gain(cls, name_ids, total_docs, doc_frequency=None, min_count=1, index_path=None):
        doc_cooccurrences = InformationGainSpark.doc_cooccurrences(name_ids, has_id=True)
        word_marginal_probs = InformationGainSpark.word_marginal_probs(name_ids, total_docs, has_id=True)

        word_info_gain = InformationGainSpark.word_info_gain(doc_cooccurrences, word_marginal_probs, min_count=min_count)

        name_words = InformationGainSpark.doc_words(name_ids, has_id=True)
        names_info_gain = InformationGainSpark.doc_scores(name_words, word_info_gain)

        return names_info_gain.mapValues(lambda values: zip(*values))

    @classmethod
    def names_geo_tfidf(cls, name_geo_ids, geo_doc_frequency, total_docs_by_geo=None, min_count=1, index_path=None):
        batch_docs_by_geo = GeoTFIDFSpark.total_docs_by_geo(name_geo_ids, has_id=True)
        if total_docs_by_geo is None:
            total_docs_by_geo = batch_docs_by_geo
        else:
            total_docs_by_geo = GeoTFIDFSpark.update_total_docs_by_geo(total_docs_by_geo, batch_docs_by_geo)

        geo_aliases = GeoTFIDFSpark.geo_aliases(total_docs_by_geo)
        total_docs_by_geo = GeoTFIDFSpark.updated_total_docs_geo_aliases(total_docs_by_geo, geo_aliases)

        name_geo_word_counts = GeoTFIDFSpark.doc_word_counts(name_geo_ids, has_id=True, geo_aliases=geo_aliases)
        name_geo_words = GeoTFIDFSpark.doc_words(name_geo_ids, has_id=True, geo_aliases=geo_aliases)

        batch_geo_doc_frequency = GeoTFIDFSpark.doc_frequency(name_geo_word_counts)

        if geo_doc_frequency is not None:
            geo_doc_frequency = GeoTFIDFSpark.update_doc_frequency(geo_doc_frequency, batch_geo_doc_frequency)
        else:
            geo_doc_frequency = batch_geo_doc_frequency

        names_geo_tfidf = GeoTFIDFSpark.doc_scores(name_geo_words, geo_doc_frequency, total_docs_by_geo)
        return names_geo_tfidf.mapValues(lambda values: zip(*values))

    @classmethod
    def names_geo_information_gain(cls, name_geo_ids, geo_doc_frequency, total_docs_by_geo=None, min_count=1, index_path=None):
        batch_docs_by_geo = GeoInformationGainSpark.total_docs_by_geo(name_geo_ids, has_id=True)
        if total_docs_by_geo is None:
            total_docs_by_geo = batch_docs_by_geo
        else:
            total_docs_by_geo = GeoInformationGainSpark.update_total_docs_by_geo(total_docs_by_geo, batch_docs_by_geo)

        geo_aliases = GeoInformationGainSpark.geo_aliases(total_docs_by_geo)
        total_docs_by_geo = GeoInformationGainSpark.updated_total_docs_geo_aliases(total_docs_by_geo, geo_aliases)

        doc_cooccurrences = GeoInformationGainSpark.doc_cooccurrences(name_geo_ids, geo_aliases=geo_aliases, has_id=True)
        word_marginal_probs = GeoInformationGainSpark.word_marginal_probs(name_geo_ids, total_docs_by_geo, has_id=True, geo_aliases=geo_aliases)

        word_info_gain = GeoInformationGainSpark.word_info_gain(doc_cooccurrences, word_marginal_probs)

        name_geo_words = GeoTFIDFSpark.doc_words(name_geo_ids, has_id=True, geo_aliases=geo_aliases)
        name_geo_info_gain = GeoInformationGainSpark.doc_scores(name_geo_words, word_info_gain)

        return name_geo_info_gain.mapValues(lambda values: zip(*values))

    @classmethod
    def dupe_sims(cls, address_ids, geo_model=True, doc_frequency=None, geo_doc_frequency=None, total_docs=0, total_docs_by_geo=None,
                  min_name_word_count=1, min_geo_name_word_count=1, name_dupe_threshold=DedupeResponse.default_name_dupe_threshold,
                  name_review_threshold=DedupeResponse.default_name_review_threshold, index_type=WordIndex.INFORMATION_GAIN, geo_model_proportion=DEFAULT_GEO_MODEL_PROPORTION,
                  with_address=True, with_unit=False, with_city_or_equivalent=False, with_small_containing_boundaries=False, with_postal_code=False,
                  name_and_address_keys=True, name_only_keys=False, address_only_keys=False,
                  with_latlon=True, fuzzy_street_name=True, with_phone_number=True):
        id_address = address_ids.map(lambda (address, uid): (uid, address))

        num_partitions = address_ids.getNumPartitions()
        name_ids = cls.names(address_ids)

        address_languages = AddressDeduperSpark.address_languages(id_address)

        batch_docs = cls.batch_doc_count(address_ids)
        total_docs += batch_docs

        if index_type == WordIndex.TFIDF:
            name_scores = cls.names_tfidf(name_ids, total_docs, doc_frequency=doc_frequency, min_count=min_name_word_count)
        elif index_type == WordIndex.INFORMATION_GAIN:
            name_scores = cls.names_information_gain(name_ids, total_docs, doc_frequency=doc_frequency, min_count=min_name_word_count)

        name_geo_scores = None

        if geo_model:
            name_geo_ids = cls.names_geo(address_ids)
            if index_type == WordIndex.TFIDF:
                name_geo_scores = cls.names_geo_tfidf(name_geo_ids, geo_doc_frequency, total_docs_by_geo, min_count=min_geo_name_word_count)
            elif index_type == WordIndex.INFORMATION_GAIN:
                name_geo_scores = cls.names_geo_information_gain(name_geo_ids, geo_doc_frequency, total_docs_by_geo, min_count=min_geo_name_word_count)

        addresses_and_languages = id_address.join(address_languages)

        address_hashes = addresses_and_languages.flatMap(lambda (uid, (address, langs)): [(h, uid) for h in NameAddressDeduper.near_dupe_hashes(address, languages=langs, with_address=with_address, with_unit=with_unit, with_city_or_equivalent=with_city_or_equivalent, \
                                                                                                                                                with_small_containing_boundaries=with_small_containing_boundaries, with_postal_code=with_postal_code, with_latlon=with_latlon,
                                                                                                                                                name_and_address_keys=name_and_address_keys, name_only_keys=name_only_keys, address_only_keys=address_only_keys)])

        address_dupe_pairs = AddressDeduperSpark.address_dupe_pairs(address_hashes, addresses_and_languages, sub_building=with_unit, fuzzy_street_name=fuzzy_street_name) \
                                                .coalesce(num_partitions)

        id_names = name_ids.map(lambda (name, uid): (uid, name))
        names_and_languages = id_names.join(address_languages)

        exact_dupe_pairs = IDPairRDD.join_pairs(address_dupe_pairs.keys(), names_and_languages) \
                                    .filter(lambda ((uid1, uid2), ((name1, langs1), (name2, langs2))): NameAddressDeduper.name_dupe_status(name1, name2, languages=AddressDeduper.combined_languages(langs1, langs2)) == duplicate_status.EXACT_DUPLICATE ) \
                                    .keys() \
                                    .coalesce(num_partitions)

        possible_dupe_pairs = address_dupe_pairs.keys().subtract(exact_dupe_pairs)

        if not geo_model:
            dupe_pair_sims = IDPairRDD.join_pairs(possible_dupe_pairs, name_scores.join(address_languages)) \
                                      .mapValues(lambda (((tokens1, scores1), languages1), ((tokens2, scores2), languages2)): cls.name_similarity(tokens1, scores1, tokens2, scores2, languages=NameAddressDeduper.combined_languages(languages1, languages2)))
        else:
            dupe_pair_sims = IDPairRDD.join_pairs(possible_dupe_pairs, name_scores.join(name_geo_scores).join(address_languages)) \
                                      .mapValues(lambda ((((tokens1, scores1), (geo_tokens1, geo_scores1)), languages1) , (((tokens2, scores2), (geo_tokens2, geo_scores2)), languages2)): cls.name_geo_dupe_similarity(tokens1, scores1, geo_scores1, tokens2, scores2, geo_scores2,
                                                                                                                                                                                                                        languages=NameAddressDeduper.combined_languages(languages1, languages2),
                                                                                                                                                                                                                        geo_model_proportion=geo_model_proportion))

        exact_dupe_sims = exact_dupe_pairs.map(lambda (uid1, uid2): ((uid1, uid2), (DedupeResponse.classifications.EXACT_DUPE, 1.0)))

        possible_dupe_sims = dupe_pair_sims.coalesce(num_partitions) \
                                           .filter(lambda ((uid1, uid2), (dupe_class, sim)): dupe_class in (duplicate_status.LIKELY_DUPLICATE, duplicate_status.NEEDS_REVIEW)) \
                                           .subtractByKey(exact_dupe_sims)

        id_address_with_phone = id_address.filter(lambda (uid, address): bool(address.get(EntityDetails.PHONE, u'').strip()))

        if (with_phone_number):
            possible_dupe_sims = IDPairRDD.join_pairs(possible_dupe_sims.keys(), id_address_with_phone) \
                                          .join(possible_dupe_sims) \
                                          .mapValues(lambda ((a1, a2), (dupe_class, sim)): (PhoneNumberDeduper.revised_dupe_class(dupe_class, a1, a2), sim)) \
                                          .coalesce(num_partitions)

        possible_dupe_sims = possible_dupe_sims.mapValues(lambda (dupe_class, sim): (DedupeResponse.string_dupe_class(dupe_classe), sim))

        all_dupe_sims = possible_dupe_sims.union(exact_dupe_sims)

        return all_dupe_sims
