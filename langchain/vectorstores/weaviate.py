"""Wrapper around weaviate vector database.""" 
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Type, Callable, Tuple
from uuid import uuid4

import datetime

import numpy as np

from langchain.docstore.document import Document
from langchain.embeddings.base import Embeddings
from langchain.utils import get_from_dict_or_env
from langchain.vectorstores.base import VectorStore
from langchain.vectorstores.utils import maximal_marginal_relevance


def _default_schema(index_name: str) -> Dict:
    return {
        "class": index_name,
        "properties": [
            {
                "name": "text",
                "dataType": ["text"],
            }
        ],
    } 


def _create_weaviate_client(**kwargs: Any) -> Any:
    client = kwargs.get("client")
    if client is not None:
        return client

    weaviate_url = get_from_dict_or_env(kwargs, "weaviate_url", "WEAVIATE_URL")

    try:
        # the weaviate api key param should not be mandatory
        weaviate_api_key = get_from_dict_or_env(
            kwargs, "weaviate_api_key", "WEAVIATE_API_KEY", None
        )
    except ValueError:
        weaviate_api_key = None

    try:
        import weaviate
    except ImportError:
        raise ValueError(
            "Could not import weaviate python  package. "
            "Please install it with `pip instal weaviate-client`"
        )

    auth = (
        weaviate.auth.AuthApiKey(api_key=weaviate_api_key)
        if weaviate_api_key is not None
        else None
    )
    client = weaviate.Client(weaviate_url, auth_client_secret=auth)

    return client


class Weaviate(VectorStore):
    """Wrapper around Weaviate vector database.

    To use, you should have the ``weaviate-client`` python package installed.

    Example:
        .. code-block:: python

            import weaviate
            from langchain.vectorstores import Weaviate
            client = weaviate.Client(url=os.environ["WEAVIATE_URL"], ...)
            weaviate = Weaviate(client, index_name, text_key)

    """

    def __init__(
        self,
        client: Any,
        index_name: str,
        text_key: str,
        embedding: Optional[Embeddings] = None,
        attributes: Optional[List[str]] = None,
        relevance_score_fn: Optional[Callable[[float], float]] = None,
    ):
        """Initialize with Weaviate client."""
        try:
            import weaviate
        except ImportError:
            raise ValueError(
                "Could not import weaviate python package. "
                "Please install it with `pip install weaviate-client`."
            )
        if not isinstance(client, weaviate.Client):
            raise ValueError(
                f"client should be an instance of weaviate.Client, got {type(client)}"
            )
        self._client = client
        self._index_name = index_name
        self._embedding = embedding
        self._text_key = text_key
        self._query_attrs = [self._text_key]
        self._relevance_score_fn = relevance_score_fn
        if attributes is not None:
            self._query_attrs.extend(attributes)

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Upload texts with metadata (properties) to Weaviate."""
        from weaviate.util import get_valid_uuid

        def json_serializable(value):
            if isinstance(value, datetime.datetime):
                return value.isoformat()
            return value

        with self._client.batch as batch:
            ids = []
            for i, doc in enumerate(texts):
                data_properties = {
                    self._text_key: doc,
                }
                if metadatas is not None:
                    for key in metadatas[i].keys():
                        data_properties[key] = json_serializable(metadatas[i][key])

                _id = get_valid_uuid(uuid4())

                if self._embedding is not None:
                    embeddings = self._embedding.embed_documents(list(doc))
                    batch.add_data_object(
                        data_object=data_properties,
                        class_name=self._index_name,
                        uuid=_id,
                        vector=embeddings[0],
                    )
                else:
                    batch.add_data_object(
                        data_object=data_properties,
                        class_name=self._index_name,
                        uuid=_id,
                    )
                ids.append(_id)
        return ids

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        """Return docs most similar to query.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.

        Returns:
            List of Documents most similar to the query.
        """
        content: Dict[str, Any] = {"concepts": [query]}
        if kwargs.get("search_distance"):
            content["certainty"] = kwargs.get("search_distance")
        query_obj = self._client.query.get(self._index_name, self._query_attrs)
        if kwargs.get("where_filter"):
            query_obj = query_obj.with_where(kwargs.get("where_filter"))
        result = query_obj.with_near_text(content).with_limit(k).do()
        if "errors" in result:
            raise ValueError(f"Error during query: {result['errors']}")
        docs = []
        for res in result["data"]["Get"][self._index_name]:
            text = res.pop(self._text_key)
            docs.append(Document(page_content=text, metadata=res))
        return docs

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Document]:
        """Look up similar documents by embedding vector in Weaviate."""
        vector = {"vector": embedding}
        query_obj = self._client.query.get(self._index_name, self._query_attrs)
        if kwargs.get("where_filter"):
            query_obj = query_obj.with_where(kwargs.get("where_filter"))
        result = query_obj.with_near_vector(vector).with_limit(k).do()
        if "errors" in result:
            raise ValueError(f"Error during query: {result['errors']}")
        docs = []
        for res in result["data"]["Get"][self._index_name]:
            text = res.pop(self._text_key)
            docs.append(Document(page_content=text, metadata=res))
        return docs

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        if self._embedding is not None:
            embedding = self._embedding.embed_query(query)
        else:
            raise ValueError(
                "max_marginal_relevance_search requires a suitable Embeddings object"
            )

        return self.max_marginal_relevance_search_by_vector(
            embedding, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult, **kwargs
        )

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: List[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        vector = {"vector": embedding}
        query_obj = self._client.query.get(self._index_name, self._query_attrs)
        if kwargs.get("where_filter"):
            query_obj = query_obj.with_where(kwargs.get("where_filter"))
        results = (
            query_obj.with_additional("vector")
            .with_near_vector(vector)
            .with_limit(fetch_k)
            .do()
        )

        payload = results["data"]["Get"][self._index_name]
        embeddings = [result["_additional"]["vector"] for result in payload]
        mmr_selected = maximal_marginal_relevance(
            np.array(embedding), embeddings, k=k, lambda_mult=lambda_mult
        )

        docs = []
        for idx in mmr_selected:
            text = payload[idx].pop(self._text_key)
            payload[idx].pop("_additional")
            meta = payload[idx]
            docs.append(Document(page_content=text, metadata=meta))
        return docs 

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        content: Dict[str, Any] = {"concepts": [query]}
        if kwargs.get("search_distance"):
            content["certainty"] = kwargs.get("search_distance")
        query_obj = self._client.query.get(self._index_name, self._query_attrs)
        result = query_obj.with_near_text(content).with_limit(k).with_additional("vector").do()
        if "errors" in result:
            raise ValueError(f"Error during query: {result['errors']}")

        docs_and_scores = []
        for res in result["data"]["Get"][self._index_name]:
            text = res.pop(self._text_key)
            score = np.dot(res["_additional"]["vector"], self._embedding.embed_query(query))
            docs_and_scores.append((Document(page_content=text, metadata=res), score))
        return docs_and_scores

    def _similarity_search_with_relevance_scores(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Return docs and relevance scores, normalized on a scale from 0 to 1.

        0 is dissimilar, 1 is most similar.
        """
        if self._relevance_score_fn is None:
            raise ValueError(
                "relevance_score_fn must be provided to"
                " Weaviate constructor to normalize scores"
            )
        docs_and_scores = self.similarity_search_with_score(query, k=k)
        return [(doc, self._relevance_score_fn(score)) for doc, score in docs_and_scores]

    @classmethod
    def from_texts(
        cls: Type[Weaviate],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> Weaviate:
        """Construct Weaviate wrapper from raw documents.

        This is a user-friendly interface that:
            1. Embeds documents.
            2. Creates a new index for the embeddings in the Weaviate instance.
            3. Adds the documents to the newly created Weaviate index.

        This is intended to be a quick way to get started.

        Example:
            .. code-block:: python

                from langchain.vectorstores.weaviate import Weaviate
                from langchain.embeddings import OpenAIEmbeddings
                embeddings = OpenAIEmbeddings()
                weaviate = Weaviate.from_texts(
                    texts,
                    embeddings,
                    weaviate_url="http://localhost:8080"
                )
        """

        client = _create_weaviate_client(**kwargs)

        from weaviate.util import get_valid_uuid

        index_name = kwargs.get("index_name", f"LangChain_{uuid4().hex}")
        embeddings = embedding.embed_documents(texts) if embedding else None
        text_key = "text"
        schema = _default_schema(index_name)
        attributes = list(metadatas[0].keys()) if metadatas else None

        # check whether the index already exists
        if not client.schema.contains(schema):
            client.schema.create_class(schema)

        with client.batch as batch:
            for i, text in enumerate(texts):
                data_properties = {
                    text_key: text,
                }
                if metadatas is not None:
                    for key in metadatas[i].keys():
                        data_properties[key] = metadatas[i][key]

                _id = get_valid_uuid(uuid4())

                # if an embedding strategy is not provided, we let
                # weaviate create the embedding. Note that this will only
                # work if weaviate has been installed with a vectorizer module
                # like text2vec-contextionary for example
                params = {
                    "uuid": _id,
                    "data_object": data_properties,
                    "class_name": index_name,
                }
                if embeddings is not None:
                    params["vector"] = embeddings[i]

                batch.add_data_object(**params)

            batch.flush()

        return cls(client, index_name, text_key, embedding, attributes)
